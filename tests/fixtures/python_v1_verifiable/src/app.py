from src.shim import Client

client = Client()


def is_flagged(text):
    response = client.create_moderation(text)
    return response["results"][0]["flagged"]
