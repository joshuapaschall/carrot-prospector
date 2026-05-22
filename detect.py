#!/usr/bin/env python3
import json
import sys
from typing import Dict, Optional
from urllib import error, request

USER_AGENT = "carrot-prospector/0.1"
TIMEOUT = 25.0


def is_carrot(domain: str) -> Dict[str, Optional[str]]:
    result: Dict[str, Optional[str]] = {
        "domain": domain,
        "is_carrot": False,
        "server_header": None,
        "status": None,
        "error": None,
    }

    req = request.Request(f"https://{domain}", headers={"User-Agent": USER_AGENT}, method="GET")
    try:
        with request.urlopen(req, timeout=TIMEOUT) as response:
            server_header = response.headers.get("server")
            result["server_header"] = server_header
            result["status"] = response.status
            if server_header and "carrot" in server_header.lower():
                result["is_carrot"] = True
    except error.HTTPError as exc:
        server_header = exc.headers.get("server") if exc.headers else None
        result["server_header"] = server_header
        result["status"] = exc.code
        if server_header and "carrot" in server_header.lower():
            result["is_carrot"] = True
        result["error"] = f"HTTPError: {exc}"
    except Exception as exc:
        result["error"] = f"{exc.__class__.__name__}: {exc}"

    return result


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python detect.py <domain> [domain2 ...]", file=sys.stderr)
        return 1
    for domain in sys.argv[1:]:
        print(json.dumps(is_carrot(domain), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
