"""Self-contained stand-in for a provider SDK, implementing BOTH the legacy
and the migrated API surface -- so the fixture's tests pass on the original
code (baseline) and on a *correctly* migrated version, while a wrong
migration fails them. Mirrors tests/fixtures/openai_v3_verifiable's shim."""


class _Moderations:
    def create(self, text):
        # migrated API: flat result shape
        return {"flagged": "bad" in text}


class Client:
    def __init__(self):
        self.moderations = _Moderations()

    def create_moderation(self, text):
        # legacy API: nested result shape
        return {"results": [{"flagged": "bad" in text}]}
