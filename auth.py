import hashlib
import json
import os
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from flask import Flask, Response, request
from werkzeug.serving import make_server


BASE_DIR = Path(__file__).resolve().parent
TOKEN_FILE = BASE_DIR / "access_token.json"
CALLBACK_URL = "http://127.0.0.1:8765/callback"
KITE_SESSION_URL = "https://api.kite.trade/session/token"
KITE_LOGIN_URL = "https://kite.zerodha.com/connect/login"


def exchange_request_token(api_key, api_secret, request_token):
    checksum = hashlib.sha256(
        f"{api_key}{request_token}{api_secret}".encode("utf-8")
    ).hexdigest()
    response = requests.post(
        KITE_SESSION_URL,
        headers={"X-Kite-Version": "3"},
        data={
            "api_key": api_key,
            "request_token": request_token,
            "checksum": checksum,
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise RuntimeError(payload.get("message", "Kite token exchange failed."))

    access_token = payload.get("data", {}).get("access_token")
    if not access_token:
        raise ValueError("Kite's response did not contain an access token.")
    return access_token


def main():
    load_dotenv(BASE_DIR / ".env")
    api_key = os.getenv("KITE_API_KEY")
    api_secret = os.getenv("KITE_API_SECRET")
    if not api_key or not api_secret:
        raise SystemExit(
            "Set KITE_API_KEY and KITE_API_SECRET in the .env file before logging in."
        )

    app = Flask(__name__)
    result = {"message": "Login callback received."}

    @app.get("/callback")
    def callback():
        request_token = request.args.get("request_token")
        if not request_token:
            result["message"] = (
                "No request_token was returned. Check the redirect URL in your "
                "Kite developer settings."
            )
            return Response(result["message"], status=400, mimetype="text/plain")

        try:
            access_token = exchange_request_token(
                api_key, api_secret, request_token
            )
        except (requests.RequestException, ValueError, RuntimeError) as error:
            result["message"] = f"Could not complete Kite login: {error}"
            return Response(result["message"], status=502, mimetype="text/plain")

        TOKEN_FILE.write_text(
            json.dumps(
                {
                    "access_token": access_token,
                    "saved_at": datetime.now().astimezone().isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        result["message"] = (
            "Kite login successful. You can close this tab and return to the "
            "dashboard."
        )
        return Response(result["message"], mimetype="text/plain")

    server = make_server("127.0.0.1", 8765, app)
    login_url = f"{KITE_LOGIN_URL}?{urlencode({'v': '3', 'api_key': api_key})}"
    print(f"Waiting for Kite login callback at {CALLBACK_URL}")
    print("If your browser does not open, visit this login URL:")
    print(login_url)
    webbrowser.open(login_url)
    server.handle_request()
    server.server_close()
    print(result["message"])
    if not TOKEN_FILE.exists():
        raise SystemExit(1)
    if not result["message"].startswith("Kite login successful"):
        raise SystemExit(1)
    print(f"Access token saved locally in {TOKEN_FILE.name}.")


if __name__ == "__main__":
    main()
