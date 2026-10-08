"""
model_doctor.py - Find out which Gemini model works for YOUR API key, and prove it end to end.

    python model_doctor.py            # list usable models + test the best one (incl. Search grounding)
    python model_doctor.py --all      # test every candidate and print which ones pass

It uses exactly the same code path as the app (GeminiVerifier), so if this prints OK the app will work.
At the end it prints the line to paste into .env.
"""
from __future__ import annotations

import argparse
import sys

from config import settings
from engine import GeminiVerifier

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="test every discovered candidate")
    args = ap.parse_args()

    if not settings.gemini_api_key:
        print("GEMINI_API_KEY is empty. Put it in .env (see .env.example) and run again.")
        return 1
    v = GeminiVerifier(settings)
    if not v.enabled:
        print("google-genai is not installed: pip install -r requirements.txt")
        return 1

    print(f"Configured in .env : {settings.gemini_model}")
    found = v.discover_models()
    print(f"Usable for this key: {len(found)} model(s)")
    for n in found[:15]:
        print("   -", n)
    if not found:
        print("   (none returned - key invalid, no access, or network problem)")

    from google.genai import types as T
    candidates = found if args.all else ([settings.gemini_model] + found)[:1 + 4]
    seen, working = set(), []
    for name in candidates:
        if name in seen:
            continue
        seen.add(name)
        try:
            r = v.client.models.generate_content(
                model=name, contents="Reply with the single word: OK",
                config=T.GenerateContentConfig(max_output_tokens=20))
            plain = (r.text or "").strip()[:20]
            try:                                    # HLRN also needs Google Search grounding
                v.client.models.generate_content(
                    model=name, contents="What is today's date in India?",
                    config=T.GenerateContentConfig(tools=[T.Tool(google_search=T.GoogleSearch())],
                                                   max_output_tokens=60))
                grounded = "grounding OK"
            except Exception as e:  # noqa: BLE001
                grounded = f"grounding FAILED ({str(e)[:80]})"
            print(f"[PASS] {name:<32} reply={plain!r}  {grounded}")
            if "FAILED" not in grounded:
                working.append(name)
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] {name:<32} {str(e)[:140]}")
        if working and not args.all:
            break

    if working:
        print(f"\nPaste this into .env, then restart the app:\n\nGEMINI_MODEL={working[0]}\n")
        return 0
    print("\nNo model passed both tests. Check the key at https://aistudio.google.com/apikey "
          "and that your account/region has Gemini API access.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
