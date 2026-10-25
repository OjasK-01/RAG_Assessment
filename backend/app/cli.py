"""Ask the RAG from the terminal.

  python -m app.cli "What is the resolution target for a P2 ticket?" [--trace]
  python -m app.cli --chat [--trace]      multi-turn: follow-ups use the conversation so far"""
import json
import sys

from app.rag import answer_with_trace, make_turn


def _show(result: dict, trace, show_trace: bool) -> None:
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if show_trace:
        print(json.dumps(trace.__dict__, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    show_trace = "--trace" in sys.argv
    args = [a for a in sys.argv[1:] if a not in ("--trace", "--chat")]
    if "--chat" in sys.argv:
        history = []
        print("Multi-turn chat. Empty line to quit.")
        while question := input("\nyou> ").strip():
            result, trace = answer_with_trace(question, account="cli", role="admin", history=history)
            _show(result, trace, show_trace)
            history.append(make_turn(result, trace))
    elif args:
        _show(*answer_with_trace(" ".join(args), account="cli", role="admin"), show_trace)
    else:
        sys.exit(__doc__)
