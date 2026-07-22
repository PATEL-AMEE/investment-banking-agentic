import json
import urllib.request

payload = json.dumps({
    "clientId": "C123",
    "txData": {"amount": 250000, "currency": "GBP"},
    "requestId": "REQ-001",
    "userId": "U-001",
    "sessionId": "S-001",
    "workflowStep": "compliance-review",
}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8000/api/agents/inspect",
    data=payload,
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req) as resp:
    print(resp.read().decode())
