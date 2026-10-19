import type { CSSProperties } from "react";
import type { CameraRuntime, EventDto } from "../api/generated/types";
import { clock } from "./time";
export const activities: Record<EventDto["activity"], string> = {
  reading: "阅读",
  writing: "书写",
  computer: "使用电脑",
  phone: "使用手机",
  away: "离开画面",
  resting: "休息",
  other: "其他活动",
  unknown: "无法判断",
};
export function activityLabel(name: string): string {
  return activities[name] ?? name;
}
export function activityStyle(name: string): CSSProperties {
  const colors: Record<string, string> = {
    reading: "#367b61",
    writing: "#4f7892",
    computer: "#647c75",
    phone: "#966f4f",
    away: "#727568",
    resting: "#a28356",
    other: "#727568",
    unknown: "#64716d",
  };
  const palette = [
    "#477e68",
    "#567a96",
    "#997449",
    "#8c6b86",
    "#6b7c52",
    "#a26e62",
    "#508c8c",
  ];
  const hash = [...name].reduce(
    (h, c) => (h * 31 + c.codePointAt(0)!) >>> 0,
    0,
  );
  return {
    "--activity": colors[name] ?? palette[hash % palette.length],
  } as CSSProperties;
}
export const analysisLabels: Record<string, string> = {
  pending: "已采集，等待分析任务",
  running: "分析进行中",
  complete: "分析完成",
  partial: "部分范围已分析",
  failed: "分析失败",
  expired: "未完成分析，现场图已过期",
};
export const evidenceLabels: Record<string, string> = {
  available: "原图可用",
  partial: "部分原图可用",
  expired: "原图已过期",
  evicted: "空间不足提前回收",
  missing: "文件缺失",
  none: "无证据图片",
  not_captured: "未采到",
  outside_schedule: "计划外",
};
export const modeLabels: Record<string, string> = {
  sparse: "稀疏采样",
  dense: "逐秒分析",
  mixed: "混合分析",
};
export const jobLabels: Record<string, string> = {
  queued: "排队中",
  running: "执行中",
  succeeded: "已完成",
  partial: "部分完成",
  failed: "失败",
  expired: "已过期",
  cancelled: "已取消",
};
export const kindLabels: Record<string, string> = {
  camera_test: "机位连接测试",
  provider_test: "模型能力测试",
  sparse_analysis: "稀疏分析",
  dense_analysis: "逐秒分析",
  dense_backfill: "逐秒补充分析",
  manual_visual: "原图重新分析",
  reconcile_minutes: "更新分钟记录",
  summarize_hour: "小时汇总",
  summarize_day: "日报汇总",
  manual_records: "按现有记录汇总",
  project_events: "更新事件视图",
  cleanup_assets: "清理到期图片",
  apply_settings: "应用设置",
};
export const phaseLabels: Record<string, string> = {
  waiting: "等待执行",
  preflight: "检查资料",
  inference: "分析图片",
  summarizing: "生成汇总",
  complete: "处理完成",
  capturing: "检查视频来源",
  applying: "应用配置",
};
export const queueLabels: Record<string, string> = {
  scheduled: "等待计划时间",
  retry_wait: "等待重试",
  dependency: "等待前置记录",
  capacity: "等待空闲容量",
  provider_paused: "模型服务暂停",
};
export function captureLabel(runtime: CameraRuntime) {
  if ((runtime.control_revision ?? 0) > (runtime.applied_revision ?? 0))
    return "命令已提交，等待应用";
  if (
    runtime.capture_state === "stopped" &&
    runtime.stop_reason === "manual_override"
  )
    return `已临时停止${runtime.override_expires_at ? `，至 ${clock(runtime.override_expires_at)}` : ""}`;
  if (runtime.capture_state === "stopped")
    return (
      (
        {
          disabled: "摄像头已停用",
          outside_schedule: "当前不在运行时段",
          manual_override: "已临时停止",
          resource_pressure: "空间不足，已暂停新图",
          deleted: "已删除机位",
        } as Record<string, string>
      )[runtime.stop_reason ?? ""] ?? "已停止"
    );
  return (
    (
      {
        online: "正在采集",
        starting: "正在连接",
        offline: "视频流断开，自动重连中",
        resource_paused: "空间不足，已暂停新图",
        error: "采集异常",
      } as Record<string, string>
    )[runtime.capture_state ?? ""] ?? "等待状态"
  );
}
