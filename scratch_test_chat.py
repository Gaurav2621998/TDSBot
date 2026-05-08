import httpx
import json

async def test_chat():
    payload = {
        "question": "What is the meaning of life?",
        "chat_id": None,
        "document_ids": []
    }
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post("http://localhost:8000/chat", json=payload, timeout=60)
            print(f"Status Code: {response.status_code}")
            if response.status_code == 200:
                print(json.dumps(response.json(), indent=2))
            else:
                print(f"Error: {response.text}")
        except Exception as e:
            print(f"Request failed: {e}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(test_chat())
