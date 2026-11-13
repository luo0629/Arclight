"""Fixed child entry point. Install limits before reading untrusted bytes."""

import json
import sys

from arclight.media.execution import command_for
from arclight.media.isolation import IsolationUnavailable, seal_process
from arclight.media.limits import restrict_process
from arclight.media.text import InvalidStoryText, decode_story


def main() -> None:
    operation, argument = sys.argv[1:]
    command = command_for(operation, argument)
    if operation == "inspect":
        # Domain schemas are outside Landlock's media-only runtime. Load the
        # fixed implementation before sealing, while input is still unread.
        from arclight.media.render_inspection import inspect_render

    restrict_process(render=operation in {"render", "inspect"})
    seal_process()
    data = sys.stdin.buffer.read(-1 if command.input_limit is None else command.input_limit + 1)
    if not data or (command.input_limit is not None and len(data) > command.input_limit):
        raise ValueError("Invalid input size")
    if operation == "story":
        result = decode_story(data, argument)
    elif operation == "preview":
        from arclight.media.render_process import render

        result = render(data, argument)
    elif operation == "render":
        from arclight.media.timeline_encoder import encode_timeline

        result = encode_timeline(data)
    elif operation == "inspect":
        result = inspect_render(data).model_dump_json().encode()
    else:
        from arclight.media.probe import inspect_media

        result = json.dumps(inspect_media(data, argument), separators=(",", ":")).encode()
    if not result or (command.output_limit is not None and len(result) > command.output_limit):
        raise ValueError("Invalid output size")
    sys.stdout.buffer.write(result)


if __name__ == "__main__":
    try:
        main()
    except MemoryError:
        sys.stderr.write("ARCLIGHT_MEDIA:MEMORY_LIMIT\n")
        raise SystemExit(1) from None
    except IsolationUnavailable:
        sys.stderr.write("ARCLIGHT_MEDIA:SANDBOX_UNAVAILABLE\n")
        raise SystemExit(1) from None
    except InvalidStoryText as exc:
        sys.stderr.write("ARCLIGHT_MEDIA:" + exc.reason + "\n")
        raise SystemExit(1) from None
    except Exception:
        sys.stderr.write("ARCLIGHT_MEDIA:INVALID_MEDIA\n")
        raise SystemExit(1) from None
