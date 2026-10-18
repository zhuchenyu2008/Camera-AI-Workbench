// 自动生成：npm run generate；请勿手工编辑。
export const operations = {
  "getHealth": {
    "method": "GET",
    "path": "/health/live",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "getSession": {
    "method": "GET",
    "path": "/api/session",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "login": {
    "method": "POST",
    "path": "/api/session",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "logout": {
    "method": "DELETE",
    "path": "/api/session",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "updatePassword": {
    "method": "PATCH",
    "path": "/api/account/password",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "listCameras": {
    "method": "GET",
    "path": "/api/cameras",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "createCamera": {
    "method": "POST",
    "path": "/api/cameras",
    "idempotent": true,
    "versioned": false,
    "binary": false
  },
  "getCamera": {
    "method": "GET",
    "path": "/api/cameras/{camera_id}",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "updateCamera": {
    "method": "PATCH",
    "path": "/api/cameras/{camera_id}",
    "idempotent": true,
    "versioned": true,
    "binary": false
  },
  "deleteCamera": {
    "method": "DELETE",
    "path": "/api/cameras/{camera_id}",
    "idempotent": true,
    "versioned": true,
    "binary": false
  },
  "previewCameraUpdate": {
    "method": "POST",
    "path": "/api/cameras/{camera_id}/preview-update",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "testCamera": {
    "method": "POST",
    "path": "/api/cameras/{camera_id}/test",
    "idempotent": true,
    "versioned": false,
    "binary": false
  },
  "startCamera": {
    "method": "POST",
    "path": "/api/cameras/{camera_id}/start",
    "idempotent": true,
    "versioned": true,
    "binary": false
  },
  "stopCamera": {
    "method": "POST",
    "path": "/api/cameras/{camera_id}/stop",
    "idempotent": true,
    "versioned": true,
    "binary": false
  },
  "resumeCameraSchedule": {
    "method": "POST",
    "path": "/api/cameras/{camera_id}/resume-schedule",
    "idempotent": true,
    "versioned": true,
    "binary": false
  },
  "getPreviewStatus": {
    "method": "GET",
    "path": "/api/cameras/{camera_id}/preview/status",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "getPreviewMedia": {
    "method": "GET",
    "path": "/api/cameras/{camera_id}/preview/{session_id}/{filename}",
    "idempotent": false,
    "versioned": false,
    "binary": true
  },
  "getSnapshot": {
    "method": "GET",
    "path": "/api/cameras/{camera_id}/snapshot",
    "idempotent": false,
    "versioned": false,
    "binary": true
  },
  "getOverview": {
    "method": "GET",
    "path": "/api/overview",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "getTimeline": {
    "method": "GET",
    "path": "/api/timeline",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "listEvents": {
    "method": "GET",
    "path": "/api/events",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "getEvent": {
    "method": "GET",
    "path": "/api/events/{event_id}",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "updateEvent": {
    "method": "PATCH",
    "path": "/api/events/{event_id}",
    "idempotent": true,
    "versioned": true,
    "binary": false
  },
  "listMinutes": {
    "method": "GET",
    "path": "/api/minutes",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "getMinute": {
    "method": "GET",
    "path": "/api/minutes/{minute_id}",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "listMinuteEvidence": {
    "method": "GET",
    "path": "/api/minutes/{minute_id}/evidence",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "getEvidenceMetadata": {
    "method": "GET",
    "path": "/api/evidence/{evidence_id}/metadata",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "getEvidence": {
    "method": "GET",
    "path": "/api/evidence/{evidence_id}",
    "idempotent": false,
    "versioned": false,
    "binary": true
  },
  "previewAnalysis": {
    "method": "POST",
    "path": "/api/analysis-jobs/preview",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "createAnalysisJob": {
    "method": "POST",
    "path": "/api/analysis-jobs",
    "idempotent": true,
    "versioned": false,
    "binary": false
  },
  "getJob": {
    "method": "GET",
    "path": "/api/jobs/{job_id}",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "listJobs": {
    "method": "GET",
    "path": "/api/jobs",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "cancelJob": {
    "method": "POST",
    "path": "/api/jobs/{job_id}/cancel",
    "idempotent": true,
    "versioned": false,
    "binary": false
  },
  "listSummaries": {
    "method": "GET",
    "path": "/api/summaries",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "getSummary": {
    "method": "GET",
    "path": "/api/summaries/{summary_id}",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "regenerateSummary": {
    "method": "POST",
    "path": "/api/summaries/regenerate",
    "idempotent": true,
    "versioned": false,
    "binary": false
  },
  "listEntities": {
    "method": "GET",
    "path": "/api/entities",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "createEntity": {
    "method": "POST",
    "path": "/api/entities",
    "idempotent": true,
    "versioned": false,
    "binary": false
  },
  "getEntity": {
    "method": "GET",
    "path": "/api/entities/{entity_id}",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "updateEntity": {
    "method": "PATCH",
    "path": "/api/entities/{entity_id}",
    "idempotent": true,
    "versioned": true,
    "binary": false
  },
  "deleteEntity": {
    "method": "DELETE",
    "path": "/api/entities/{entity_id}",
    "idempotent": true,
    "versioned": true,
    "binary": false
  },
  "uploadReference": {
    "method": "POST",
    "path": "/api/entities/{entity_id}/references",
    "idempotent": true,
    "versioned": true,
    "binary": false
  },
  "promoteReference": {
    "method": "POST",
    "path": "/api/entities/{entity_id}/references/from-evidence",
    "idempotent": true,
    "versioned": true,
    "binary": false
  },
  "updateReference": {
    "method": "PATCH",
    "path": "/api/reference-assets/{asset_id}",
    "idempotent": true,
    "versioned": true,
    "binary": false
  },
  "deleteReference": {
    "method": "DELETE",
    "path": "/api/reference-assets/{asset_id}",
    "idempotent": true,
    "versioned": true,
    "binary": false
  },
  "replaceReference": {
    "method": "PUT",
    "path": "/api/reference-assets/{asset_id}/content",
    "idempotent": true,
    "versioned": true,
    "binary": false
  },
  "getReferenceContent": {
    "method": "GET",
    "path": "/api/reference-assets/{asset_id}/content",
    "idempotent": false,
    "versioned": false,
    "binary": true
  },
  "listProviders": {
    "method": "GET",
    "path": "/api/providers",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "createProvider": {
    "method": "POST",
    "path": "/api/providers",
    "idempotent": true,
    "versioned": false,
    "binary": false
  },
  "updateProvider": {
    "method": "PATCH",
    "path": "/api/providers/{provider_id}",
    "idempotent": true,
    "versioned": true,
    "binary": false
  },
  "deleteProvider": {
    "method": "DELETE",
    "path": "/api/providers/{provider_id}",
    "idempotent": true,
    "versioned": true,
    "binary": false
  },
  "getProviderPricing": {
    "method": "GET",
    "path": "/api/providers/{provider_id}/pricing",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "updateProviderPricing": {
    "method": "PUT",
    "path": "/api/providers/{provider_id}/pricing",
    "idempotent": true,
    "versioned": true,
    "binary": false
  },
  "testProvider": {
    "method": "POST",
    "path": "/api/providers/test",
    "idempotent": true,
    "versioned": false,
    "binary": false
  },
  "getSettings": {
    "method": "GET",
    "path": "/api/settings",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "updateSettings": {
    "method": "PATCH",
    "path": "/api/settings",
    "idempotent": true,
    "versioned": true,
    "binary": false
  },
  "previewSettings": {
    "method": "POST",
    "path": "/api/settings/preview",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "getStatus": {
    "method": "GET",
    "path": "/api/status",
    "idempotent": false,
    "versioned": false,
    "binary": false
  },
  "getUsage": {
    "method": "GET",
    "path": "/api/usage",
    "idempotent": false,
    "versioned": false,
    "binary": false
  }
} as const;
