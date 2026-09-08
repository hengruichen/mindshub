from __future__ import annotations

from copy import deepcopy
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.check_api_reference import (
    ContractParseError,
    compare_api_reference,
    main,
)


REFERENCE = """
<div class="endpoint" data-success-status="200" data-response-kind="json">
  <div class="endpoint-header">
    <span class="method get">GET</span>
    <span class="endpoint-path" data-path="/widgets/{id}/">Widget</span>
  </div>
  <pre data-response-body="json">{
    <span class="hl" data-nullable="false">"items"</span>: [
      {
        <span class="hl" data-nullable="false">"userId"</span>: "user-1",
        <span class="hl" data-nullable="false">"active"</span>: true,
      },
    ],
    <span class="hl" data-nullable="false"
          data-type="array">"notes"</span>: [ /* none */ ],
    <span class="hl" data-nullable="true" data-type="string">"next"</span>: null,
    <span class="hl" data-nullable="false">"url"</span>:
      "https://example.com/a//b", // preserved URL
  }</pre>
</div>
"""

OPENAPI = {
    "openapi": "3.1.0",
    "paths": {
        "/api/v1/widgets/{widget_id}": {
            "get": {
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/WidgetPage"}
                            }
                        }
                    }
                }
            }
        }
    },
    "components": {
        "schemas": {
            "WidgetPage": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/Widget"},
                    },
                    "notes": {"type": "array", "items": {"type": "string"}},
                    "next": {
                        "anyOf": [{"type": "string"}, {"type": "null"}]
                    },
                    "url": {"type": "string"},
                },
            },
            "Widget": {
                "allOf": [
                    {"$ref": "#/components/schemas/Identity"},
                    {
                        "type": "object",
                        "properties": {"active": {"type": "boolean"}},
                    },
                ]
            },
            "Identity": {
                "type": "object",
                "properties": {"userId": {"type": "string"}},
            },
        }
    },
}


class ApiReferenceComparisonTests(unittest.TestCase):
    def test_matching_contract_accepts_json_comments_and_trailing_commas(self):
        self.assertEqual(compare_api_reference(REFERENCE, OPENAPI), [])

    def test_documented_operation_must_exist(self):
        reference = REFERENCE.replace("/widgets/{id}/", "/missing/{id}")

        self.assertEqual(
            compare_api_reference(reference, OPENAPI),
            ["GET /missing/{id}: documented operation is absent from OpenAPI"],
        )

    def test_duplicate_documented_operation_is_rejected_after_normalization(self):
        duplicate = REFERENCE.replace("/widgets/{id}/", "/api/v1/widgets/{other}")

        with self.assertRaisesRegex(
            ContractParseError,
            r"duplicate documented operation GET /api/v1/widgets/\{other\}",
        ):
            compare_api_reference(REFERENCE + duplicate, OPENAPI)

    def test_field_name_drift_reports_both_sides(self):
        reference = REFERENCE.replace('>"userId"<', '>"user_id"<')

        self.assertEqual(
            compare_api_reference(reference, OPENAPI),
            [
                "GET /widgets/{id}/ response.items[].userId: OpenAPI field is "
                "absent from docs",
                "GET /widgets/{id}/ response.items[].user_id: documented field "
                "is absent from OpenAPI",
            ],
        )

    def test_field_type_drift_is_reported(self):
        reference = REFERENCE.replace('"https://example.com/a//b"', "7")

        self.assertEqual(
            compare_api_reference(reference, OPENAPI),
            ["GET /widgets/{id}/ response.url: docs say integer, OpenAPI says string"],
        )

    def test_docs_nullable_openapi_non_nullable_is_reported(self):
        reference = REFERENCE.replace(
            'data-nullable="false">"url"',
            'data-nullable="true">"url"',
        )

        self.assertEqual(
            compare_api_reference(reference, OPENAPI),
            [
                "GET /widgets/{id}/ response.url: docs say nullable, OpenAPI says "
                "non-nullable"
            ],
        )

    def test_openapi_nullable_docs_non_nullable_is_reported(self):
        reference = REFERENCE.replace(
            'data-nullable="true" data-type="string">"next"',
            'data-nullable="false" data-type="string">"next"',
        )

        self.assertEqual(
            compare_api_reference(reference, OPENAPI),
            [
                "GET /widgets/{id}/ response.next: docs say non-nullable, OpenAPI "
                "says nullable"
            ],
        )

    def test_success_status_drift_is_reported(self):
        reference = REFERENCE.replace(
            'data-success-status="200"', 'data-success-status="201"'
        )

        self.assertEqual(
            compare_api_reference(reference, OPENAPI),
            ["GET /widgets/{id}/: docs say success status 201, OpenAPI has 200"],
        )

    def test_response_body_kind_drift_is_reported(self):
        reference = REFERENCE.replace(
            'data-response-kind="json"', 'data-response-kind="sse"'
        ).replace(' data-response-body="json"', "")

        self.assertEqual(
            compare_api_reference(reference, OPENAPI),
            ["GET /widgets/{id}/: docs say response kind sse, OpenAPI says json"],
        )

    def test_conditional_json_and_sse_response_is_supported(self):
        reference = REFERENCE.replace(
            'data-response-kind="json"', 'data-response-kind="json sse"'
        )
        openapi = deepcopy(OPENAPI)
        content = openapi["paths"]["/api/v1/widgets/{widget_id}"]["get"][
            "responses"
        ]["200"]["content"]
        content["text/event-stream"] = {"schema": {"type": "string"}}

        self.assertEqual(compare_api_reference(reference, openapi), [])

    def test_empty_openapi_json_schema_is_rejected(self):
        openapi = deepcopy(OPENAPI)
        openapi["paths"]["/api/v1/widgets/{widget_id}"]["get"]["responses"][
            "200"
        ]["content"]["application/json"]["schema"] = {}

        self.assertEqual(
            compare_api_reference(REFERENCE, openapi),
            [
                "GET /widgets/{id}/: OpenAPI success response has an empty JSON "
                "schema"
            ],
        )

    def test_bodyless_success_response_is_supported(self):
        reference = """
        <div class="endpoint" data-success-status="204" data-response-kind="empty">
          <span class="method delete">DELETE</span>
          <span class="endpoint-path" data-path="/widgets/{id}">Widget</span>
        </div>
        """
        openapi = {
            "paths": {
                "/api/v1/widgets/{widget_id}": {
                    "delete": {"responses": {"204": {"description": "Deleted"}}}
                }
            }
        }

        self.assertEqual(compare_api_reference(reference, openapi), [])

    def test_missing_nullability_metadata_is_rejected(self):
        reference = REFERENCE.replace(
            ' data-nullable="false">"active"',
            '>"active"',
        )

        with self.assertRaisesRegex(
            ContractParseError,
            r"response.items\[\]\.active: missing data-nullable",
        ):
            compare_api_reference(reference, OPENAPI)

    def test_null_and_empty_values_require_explicit_types(self):
        for field in ("notes", "next"):
            with self.subTest(field=field):
                field_type = "array" if field == "notes" else "string"
                reference = REFERENCE.replace(
                    f' data-type="{field_type}">"{field}"',
                    f'>"{field}"',
                )
                with self.assertRaisesRegex(
                    ContractParseError,
                    rf"response\.{field}: null or empty values require data-type",
                ):
                    compare_api_reference(reference, OPENAPI)

    def test_malformed_response_example_is_rejected(self):
        reference = REFERENCE.replace('>"url"</span>:', '>"url"</span>')

        with self.assertRaisesRegex(
            ContractParseError,
            r"GET /widgets/\{id\}/: malformed JSON response example",
        ):
            compare_api_reference(reference, OPENAPI)

    def test_cli_exits_nonzero_with_operation_and_field_diagnostic(self):
        reference = REFERENCE.replace('"https://example.com/a//b"', "7")
        with TemporaryDirectory() as directory:
            html = Path(directory) / "api.html"
            html.write_text(reference, encoding="utf-8")
            stderr = StringIO()
            with patch(
                "scripts.check_api_reference.load_openapi", return_value=OPENAPI
            ), redirect_stderr(stderr):
                result = main(["--html", str(html), "--core-api", directory])

        self.assertEqual(result, 1)
        self.assertIn(
            "GET /widgets/{id}/ response.url: docs say integer, OpenAPI says string",
            stderr.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
