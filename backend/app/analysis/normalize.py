from __future__ import annotations

from collections import defaultdict

from app.activities.names import match_activity_name
from app.errors import AppError
from app.schemas import VisualOutput
from app.timeutils import intersect, iso, subtract, timestamp, union


def validate_visual(
    output: VisualOutput,
    frames: list[dict],
    members: list[dict],
    labels: list[str],
) -> None:
    evidence = {frame["id"]: frame for frame in frames}
    people = {m["entity_id"] for m in members if m["kind"] == "person"}
    minute_starts = {timestamp(f["captured_at"]) // 60000 * 60000 for f in frames}
    seen = set()
    for minute in output.minutes:
        start = timestamp(minute.minute_start)
        if start not in minute_starts or start % 60000 or start in seen:
            raise AppError(422, "INVALID_MODEL_REFERENCES", "模型返回了重复或范围外的分钟")
        seen.add(start)
        used_ids = set(minute.unresolved_evidence_ids)
        for observation in minute.observations:
            ids = set(observation.supporting_evidence_ids) | {
                observation.start_evidence_id,
                observation.end_evidence_id,
            }
            used_ids |= ids
            if not ids <= evidence.keys():
                raise AppError(422, "INVALID_MODEL_REFERENCES", "模型引用了输入以外的现场证据")
            if (
                observation.subject_id is not None
                and observation.subject_id not in people
                or observation.object_ids
                or observation.object_candidate_keys
                or (bool(labels) and not set(observation.labels) <= set(labels))
            ):
                raise AppError(422, "INVALID_MODEL_REFERENCES", "模型引用了参考集以外的主体、物品或标签")
            if (
                observation.identity_status == "matched"
                and observation.subject_id is None
                or observation.identity_status == "unknown"
                and observation.subject_id is not None
            ):
                raise AppError(422, "INVALID_MODEL_REFERENCES", "模型身份状态与主体不一致")
            a, b = (
                timestamp(evidence[observation.start_evidence_id]["captured_at"]),
                timestamp(evidence[observation.end_evidence_id]["captured_at"]),
            )
            if a > b or not all(a <= timestamp(evidence[e]["captured_at"]) <= b for e in ids):
                raise AppError(422, "INVALID_MODEL_REFERENCES", "模型证据端点顺序错误")
        if not used_ids <= evidence.keys() or any(
            timestamp(evidence[e]["captured_at"]) // 60000 * 60000 != start for e in used_ids
        ):
            raise AppError(422, "INVALID_MODEL_REFERENCES", "模型引用跨分钟或无效证据")


def normalize(
    outputs: list[dict],
    frames: list[dict],
    captured: list[tuple[int, int]],
    *,
    max_gap_seconds=30,
    blocked: list[tuple[int, int]] | None = None,
    support="dense",
    activity_catalog: list[dict] | None = None,
) -> tuple[list[dict], bool]:
    evidence = {f["id"]: f for f in frames}
    anchors = defaultdict(list)
    review = False
    for output in outputs:
        for minute in output["minutes"]:
            review |= bool(minute["review_reasons"])
            for evidence_id in minute["unresolved_evidence_ids"]:
                if evidence_id in evidence:
                    slot = timestamp(evidence[evidence_id]["captured_at"]) // 1000 * 1000
                    anchors[(None, "unknown", None)].append(
                        (
                            slot,
                            evidence_id,
                            {
                                "activity": "unknown",
                                "labels": [],
                                "object_ids": [],
                                "observation": "模型明确无法判断此现场证据",
                            },
                        )
                    )
            for observation in minute["observations"]:
                review |= observation["needs_dense_review"]
                observation = observation | {
                    "activity": match_activity_name(observation["activity"], activity_catalog)
                }
                for evidence_id in set(
                    observation["supporting_evidence_ids"]
                    + [observation["start_evidence_id"], observation["end_evidence_id"]]
                ):
                    if evidence_id in evidence:
                        frame = evidence[evidence_id]
                        slot = timestamp(frame["captured_at"]) // 1000 * 1000
                        anchors[
                            (
                                observation["subject_id"],
                                observation["identity_status"],
                                observation.get("subject_candidate_key")
                                if observation["subject_id"] is None
                                else None,
                            )
                        ].append((slot, evidence_id, observation))
    # All evidence points are used to reject an edge inference when another
    # activity, away, or unknown is explicitly present in the same minute.
    all_points_by_minute = defaultdict(list)
    for raw in anchors.values():
        for point in raw:
            all_points_by_minute[point[0] // 60000 * 60000].append(point)

    segments = []
    for (subject, identity, _candidate), raw in anchors.items():
        unique = {
            (slot, evidence_id, item["activity"]): (slot, evidence_id, item)
            for slot, evidence_id, item in raw
        }
        points = sorted(unique.values(), key=lambda p: (p[0], p[1], p[2]["activity"]))
        intervals = []
        for slot, evidence_id, item in points:
            intervals.append((slot, slot + 1000, [evidence_id], item, {}, False))
        for left, right in zip(points, points[1:]):
            a, b = left[0], right[0]
            if b <= a or b - a > max_gap_seconds * 1000:
                continue
            full = sum(y - x for x, y in intersect(captured, [(a, b + 1000)])) == b + 1000 - a
            unresolved = blocked and intersect(blocked, [(a, b + 1000)])
            if not full or unresolved:
                continue
            if left[2]["activity"] == right[2]["activity"]:
                intervals.append((a, b + 1000, [left[1], right[1]], left[2], {}, False))
            else:
                review = True
                middle = ((a + b) // 2000) * 1000
                intervals.extend(
                    [
                        (
                            a,
                            max(a + 1000, middle),
                            [left[1], right[1]],
                            left[2],
                            {"end_lower": iso(a), "end_upper": iso(b)},
                            False,
                        ),
                        (
                            max(a + 1000, middle),
                            b + 1000,
                            [left[1], right[1]],
                            right[2],
                            {"start_lower": iso(a), "start_upper": iso(b)},
                            False,
                        ),
                    ]
                )

        # The requested rule: matching evidence near both edges of one minute
        # can cover the whole minute, while the missing middle remains visible
        # in coverage as a capture gap or analysis gap.
        by_minute = defaultdict(list)
        for point in points:
            by_minute[point[0] // 60000 * 60000].append(point)
        for minute_start, local in by_minute.items():
            left_edge = [p for p in local if p[0] < minute_start + 15000]
            right_edge = [p for p in local if p[0] >= minute_start + 45000]
            if not left_edge or not right_edge:
                continue
            activity = left_edge[0][2]["activity"]
            right_match = [p for p in right_edge if p[2]["activity"] == activity]
            if activity in {"unknown", "away"} or not right_match:
                continue
            conflict = any(item[2]["activity"] not in {activity} for item in all_points_by_minute[minute_start])
            minute_range = [(minute_start, minute_start + 60000)]
            if conflict or (blocked and intersect(blocked, minute_range)):
                review = True
                continue
            item = right_match[-1][2]
            intervals.append(
                (
                    minute_start,
                    minute_start + 60000,
                    sorted({p[1] for p in left_edge + right_match}),
                    item,
                    {"start_lower": iso(left_edge[0][0]), "end_upper": iso(right_match[-1][0] + 1000)},
                    True,
                )
            )

        grouped = defaultdict(list)
        for a, b, ids, item, boundary, estimated in intervals:
            key = (item["activity"], tuple(item["labels"]), tuple(item["object_ids"]), estimated)
            grouped[key].append((a, b, ids, item, boundary, estimated))
        for _, spans in grouped.items():
            merged = union([(a, b) for a, b, *_ in spans])
            for a, b in merged:
                related = [span for span in spans if span[0] < b and span[1] > a]
                item = related[0][3]
                boundary = {k: v for span in related for k, v in span[4].items()}
                estimated = any(span[5] for span in related)
                ranges = subtract([(a, b)], captured) if estimated else intersect([(a, b)], captured)
                for start, end in ranges:
                    segments.append(
                        dict(
                            subject_id=subject if identity == "matched" else None,
                            identity_status=identity,
                            activity=item["activity"],
                            start_at_ms=start,
                            end_at_ms=end,
                            origin="ai",
                            support=support,
                            boundary_json=boundary,
                            evidence_ids_json=sorted({e for span in related for e in span[2]}),
                            details_json={
                                "labels": item["labels"],
                                "object_ids": [],
                                "observation": item["observation"],
                                "subject_candidate_key": item.get("subject_candidate_key"),
                                "object_candidate_keys": [],
                                "estimated_ms": end - start if estimated else 0,
                                "inference_mode": "edge_estimated" if estimated else "observed",
                            },
                        )
                    )
    return segments, review
