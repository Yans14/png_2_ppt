from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from pydantic import BaseModel

from editable_pptx.openai_responses import (
    OpenAIResponsesError,
    request_structured_response,
)


class ExamplePayload(BaseModel):
    answer: str


class FakeHTTPResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class OpenAIResponsesTests(unittest.TestCase):
    @patch("editable_pptx.openai_responses.time.sleep")
    @patch("editable_pptx.openai_responses.urllib.request.urlopen")
    def test_background_response_is_polled_until_complete(self, urlopen, _sleep) -> None:
        urlopen.side_effect = [
            FakeHTTPResponse({"id": "resp/test", "status": "queued"}),
            FakeHTTPResponse({"id": "resp/test", "status": "in_progress"}),
            FakeHTTPResponse(
                {
                    "id": "resp/test",
                    "status": "completed",
                    "output_text": '{"answer":"done"}',
                }
            ),
        ]

        result = request_structured_response(
            ExamplePayload,
            schema_name="example_payload",
            system_text="Return an answer.",
            user_text="Go.",
            image_paths=[],
            model="gpt-5.5",
            api_key="test-key",
            timeout_seconds=30,
            poll_interval_seconds=0.1,
        )

        self.assertEqual(result.answer, "done")
        self.assertEqual(urlopen.call_count, 3)
        first_request = urlopen.call_args_list[0].args[0]
        self.assertEqual(first_request.method, "POST")
        self.assertTrue(json.loads(first_request.data)["background"])
        poll_request = urlopen.call_args_list[1].args[0]
        self.assertEqual(poll_request.method, "GET")
        self.assertTrue(poll_request.full_url.endswith("/responses/resp%2Ftest"))

    @patch("editable_pptx.openai_responses.time.sleep")
    @patch("editable_pptx.openai_responses.urllib.request.urlopen")
    def test_background_failure_raises_clear_error(self, urlopen, _sleep) -> None:
        urlopen.side_effect = [
            FakeHTTPResponse({"id": "resp_123", "status": "queued"}),
            FakeHTTPResponse(
                {
                    "id": "resp_123",
                    "status": "failed",
                    "error": {"message": "generation failed"},
                }
            ),
        ]

        with self.assertRaisesRegex(OpenAIResponsesError, "generation failed"):
            request_structured_response(
                ExamplePayload,
                schema_name="example_payload",
                system_text="Return an answer.",
                user_text="Go.",
                image_paths=[],
                model="gpt-5.5",
                api_key="test-key",
                timeout_seconds=30,
                poll_interval_seconds=0.1,
            )

    @patch("editable_pptx.openai_responses.urllib.request.urlopen")
    def test_invalid_structured_output_is_regenerated_with_feedback(self, urlopen) -> None:
        urlopen.side_effect = [
            FakeHTTPResponse({"status": "completed", "output_text": '{"wrong":"field"}'}),
            FakeHTTPResponse({"status": "completed", "output_text": '{"answer":"fixed"}'}),
        ]

        result = request_structured_response(
            ExamplePayload,
            schema_name="example_payload",
            system_text="Return an answer.",
            user_text="Go.",
            image_paths=[],
            model="gpt-5.6-luna",
            api_key="test-key",
            background=False,
            max_validation_retries=1,
        )

        self.assertEqual(result.answer, "fixed")
        self.assertEqual(urlopen.call_count, 2)
        retry_request = urlopen.call_args_list[1].args[0]
        retry_body = json.loads(retry_request.data)
        retry_text = retry_body["input"][-1]["content"][0]["text"]
        self.assertIn("previous response failed local schema validation", retry_text)

    @patch("editable_pptx.openai_responses.urllib.request.urlopen")
    def test_insufficient_quota_is_not_retried(self, urlopen) -> None:
        payload = {
            "error": {
                "message": "quota exhausted",
                "type": "insufficient_quota",
                "code": "insufficient_quota",
            }
        }
        urlopen.side_effect = urllib.error.HTTPError(
            "https://api.openai.com/v1/responses",
            429,
            "Too Many Requests",
            {},
            io.BytesIO(json.dumps(payload).encode("utf-8")),
        )

        with self.assertRaisesRegex(OpenAIResponsesError, "insufficient_quota"):
            request_structured_response(
                ExamplePayload,
                schema_name="example_payload",
                system_text="Return an answer.",
                user_text="Go.",
                image_paths=[],
                model="gpt-5.5",
                api_key="test-key",
                background=False,
            )

        self.assertEqual(urlopen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
