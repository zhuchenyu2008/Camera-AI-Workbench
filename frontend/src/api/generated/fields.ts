// 自动生成自 OpenAPI 字段约束；请勿手工编辑。
export const fields = {
  "SettingsConfig": {
    "timezone": {
      "type": "string",
      "const": "Asia/Shanghai",
      "title": "Timezone",
      "default": "Asia/Shanghai"
    },
    "capture_fps": {
      "type": "integer",
      "const": 1,
      "title": "Capture Fps",
      "default": 1
    },
    "frame_max_long_edge": {
      "type": "integer",
      "maximum": 1920,
      "minimum": 640,
      "title": "Frame Max Long Edge",
      "default": 1280
    },
    "frame_retention_minutes": {
      "type": "integer",
      "maximum": 10080,
      "minimum": 1,
      "title": "Frame Retention Minutes",
      "default": 60
    },
    "cleanup_interval_seconds": {
      "type": "integer",
      "maximum": 60,
      "minimum": 10,
      "title": "Cleanup Interval Seconds",
      "default": 60
    },
    "sparse_sample_offsets_seconds": {
      "items": {
        "type": "integer",
        "maximum": 59,
        "minimum": 0
      },
      "type": "array",
      "maxItems": 60,
      "minItems": 4,
      "title": "Sparse Sample Offsets Seconds",
      "default": [
        0,
        20,
        40,
        59
      ]
    },
    "sparse_submit_interval_minutes": {
      "type": "integer",
      "maximum": 10,
      "minimum": 1,
      "title": "Sparse Submit Interval Minutes",
      "default": 5
    },
    "pre_trigger_seconds": {
      "type": "integer",
      "maximum": 300,
      "minimum": 30,
      "title": "Pre Trigger Seconds",
      "default": 60
    },
    "stable_before_settling_seconds": {
      "type": "integer",
      "maximum": 300,
      "minimum": 30,
      "title": "Stable Before Settling Seconds",
      "default": 60
    },
    "post_trigger_seconds": {
      "type": "integer",
      "maximum": 120,
      "minimum": 10,
      "title": "Post Trigger Seconds",
      "default": 30
    },
    "merge_gap_seconds": {
      "type": "integer",
      "maximum": 60,
      "minimum": 0,
      "title": "Merge Gap Seconds",
      "default": 30
    },
    "max_inference_gap_seconds": {
      "type": "integer",
      "maximum": 30,
      "minimum": 1,
      "title": "Max Inference Gap Seconds",
      "default": 30
    },
    "motion_pixel_delta": {
      "type": "integer",
      "maximum": 80,
      "minimum": 5,
      "title": "Motion Pixel Delta",
      "default": 15
    },
    "motion_area_ratio": {
      "type": "number",
      "maximum": 0.1,
      "minimum": 0.001,
      "title": "Motion Area Ratio",
      "default": 0.005
    },
    "motion_single_frame_ratio": {
      "type": "number",
      "maximum": 0.5,
      "minimum": 0.001,
      "title": "Motion Single Frame Ratio",
      "default": 0.03
    },
    "ai_concurrency": {
      "type": "integer",
      "maximum": 4,
      "minimum": 1,
      "title": "Ai Concurrency",
      "default": 2
    },
    "max_capture_images_per_request": {
      "type": "integer",
      "maximum": 100,
      "minimum": 1,
      "title": "Max Capture Images Per Request",
      "default": 20
    },
    "max_total_images_per_request": {
      "type": "integer",
      "maximum": 256,
      "minimum": 2,
      "title": "Max Total Images Per Request",
      "default": 32
    },
    "max_request_body_mib": {
      "type": "integer",
      "maximum": 64,
      "minimum": 1,
      "title": "Max Request Body Mib",
      "default": 20
    },
    "ai_timeout_seconds": {
      "type": "integer",
      "maximum": 300,
      "minimum": 15,
      "title": "Ai Timeout Seconds",
      "default": 120
    },
    "ai_retry_count": {
      "type": "integer",
      "maximum": 3,
      "minimum": 0,
      "title": "Ai Retry Count",
      "default": 3
    },
    "hourly_summary_delay_minutes": {
      "type": "integer",
      "maximum": 60,
      "minimum": 1,
      "title": "Hourly Summary Delay Minutes",
      "default": 5
    },
    "daily_summary_local_time": {
      "type": "string",
      "pattern": "^(?:[01]\\d|2[0-3]):[0-5]\\d$",
      "title": "Daily Summary Local Time",
      "default": "00:10"
    },
    "source_stale_seconds": {
      "type": "integer",
      "maximum": 30,
      "minimum": 5,
      "title": "Source Stale Seconds",
      "default": 10
    },
    "media_quota_gb": {
      "type": "number",
      "maximum": 1000,
      "exclusiveMinimum": 0,
      "title": "Media Quota Gb",
      "default": 4
    },
    "reference_quota_gb": {
      "type": "number",
      "maximum": 100,
      "exclusiveMinimum": 0,
      "title": "Reference Quota Gb",
      "default": 0.2
    },
    "disk_pressure_free_gb": {
      "type": "number",
      "exclusiveMinimum": 0,
      "title": "Disk Pressure Free Gb",
      "default": 3
    },
    "disk_stop_free_gb": {
      "type": "number",
      "minimum": 0.2,
      "title": "Disk Stop Free Gb",
      "default": 1
    },
    "detailed_audit_days": {
      "type": "integer",
      "maximum": 30,
      "minimum": 1,
      "title": "Detailed Audit Days",
      "default": 7
    },
    "vision_provider_id": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Vision Provider Id"
    },
    "summary_provider_id": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Summary Provider Id"
    },
    "primary_subject_id": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Primary Subject Id",
      "deprecated": true
    },
    "auto_discovery_enabled": {
      "type": "boolean",
      "title": "Auto Discovery Enabled",
      "default": true
    },
    "activity_labels": {
      "items": {
        "type": "string",
        "maxLength": 64,
        "minLength": 1
      },
      "type": "array",
      "maxItems": 100,
      "title": "Activity Labels",
      "default": []
    }
  },
  "ProviderWrite": {
    "name": {
      "type": "string",
      "maxLength": 64,
      "minLength": 1,
      "title": "Name"
    },
    "base_url": {
      "type": "string",
      "maxLength": 2048,
      "title": "Base Url"
    },
    "model": {
      "type": "string",
      "maxLength": 200,
      "minLength": 1,
      "title": "Model"
    },
    "api_key": {
      "anyOf": [
        {
          "type": "string",
          "maxLength": 4096,
          "minLength": 1
        },
        {
          "type": "null"
        }
      ],
      "title": "Api Key"
    },
    "api_key_action": {
      "type": "string",
      "enum": [
        "keep",
        "clear"
      ],
      "title": "Api Key Action",
      "default": "keep"
    },
    "timeout_seconds": {
      "type": "integer",
      "maximum": 300,
      "minimum": 15,
      "title": "Timeout Seconds",
      "default": 120
    },
    "max_total_images": {
      "type": "integer",
      "maximum": 256,
      "minimum": 2,
      "title": "Max Total Images",
      "default": 32
    },
    "max_request_body_mib": {
      "type": "integer",
      "maximum": 64,
      "minimum": 1,
      "title": "Max Request Body Mib",
      "default": 20
    },
    "max_capture_images": {
      "type": "integer",
      "maximum": 100,
      "minimum": 1,
      "title": "Max Capture Images",
      "default": 20
    },
    "output_token_limit": {
      "type": "integer",
      "maximum": 65536,
      "minimum": 128,
      "title": "Output Token Limit",
      "default": 4000
    },
    "output_token_parameter": {
      "type": "string",
      "enum": [
        "max_completion_tokens",
        "max_tokens"
      ],
      "title": "Output Token Parameter",
      "default": "max_completion_tokens"
    },
    "response_format_mode": {
      "type": "string",
      "enum": [
        "json_schema",
        "json_object",
        "prompt_json"
      ],
      "title": "Response Format Mode",
      "default": "json_schema"
    },
    "detail_mode": {
      "type": "string",
      "enum": [
        "auto",
        "low",
        "high",
        "omit"
      ],
      "title": "Detail Mode",
      "default": "auto"
    }
  },
  "ProviderPricingConfig": {
    "currency": {
      "type": "string",
      "enum": [
        "CNY",
        "USD"
      ],
      "title": "Currency"
    },
    "input_price": {
      "type": "string",
      "title": "Input Price"
    },
    "output_price": {
      "type": "string",
      "title": "Output Price"
    },
    "cached_input_price": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Cached Input Price"
    },
    "source_note": {
      "type": "string",
      "maxLength": 500,
      "minLength": 1,
      "title": "Source Note"
    }
  },
  "ProviderPricingWrite": {
    "provider_revision": {
      "type": "integer",
      "minimum": 1,
      "title": "Provider Revision"
    },
    "pricing_revision": {
      "type": "integer",
      "minimum": 1,
      "title": "Pricing Revision"
    },
    "clear": {
      "type": "boolean",
      "title": "Clear",
      "default": false
    },
    "currency": {
      "anyOf": [
        {
          "type": "string",
          "enum": [
            "CNY",
            "USD"
          ]
        },
        {
          "type": "null"
        }
      ],
      "title": "Currency"
    },
    "input_price": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Input Price"
    },
    "output_price": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Output Price"
    },
    "cached_input_price": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Cached Input Price"
    },
    "source_note": {
      "anyOf": [
        {
          "type": "string",
          "maxLength": 500,
          "minLength": 1
        },
        {
          "type": "null"
        }
      ],
      "title": "Source Note"
    }
  }
} as const;
