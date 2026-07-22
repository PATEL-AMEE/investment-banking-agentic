import json
import urllib.request

payload = json.dumps({
    "clientId": "C789",
    "txData": {"amount": 650000, "currency": "USD"},
    "requestId": "REQ-003",
    "userId": "U-003",
    "sessionId": "S-003",
    "workflowStep": "human-review",
}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8000/api/agents/inspect",
    data=payload,
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req) as resp:
    print(resp.read().decode())
