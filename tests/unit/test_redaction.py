"""Unit tests for :mod:`depfix.redaction`.

The load-bearing property is round-tripping: a fixer returns the whole file,
so a placeholder that isn't swapped back deletes a credential. Every test
here is really asking one of two questions — "was the secret hidden?" and
"can it be put back, or does the guard refuse?".

Test secrets are assembled at runtime so secret scanners (gitleaks,
GitHub push protection) don't flag this file.
"""

from __future__ import annotations

import pytest

from depfix.core.models import FileUsage, Usage
from depfix.redaction import (
    PLACEHOLDER_RE,
    RedactionError,
    redact,
    redact_file_usage,
    redact_text,
)

_OPENAI = "sk-proj-" + "abcdefghijklmnopqrstuvwxyz0123456789ABCD"
_STRIPE = "sk_live_" + "51H8xKlMnOpQrStUvWxYz0123"
_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEAwJz9Xy1abcdefghijklmnopqrstuvwxyz0123456789ABCDEF\n"
    "-----END RSA PRIVATE KEY-----"
)


# -- detection ---------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "sk-proj-" + "abcdefghijklmnopqrstuvwxyz0123456789ABCD",
        "sk_live_" + "51H8xKlMnOpQrStUvWxYz0123",
        "whsec_" + "abcdefghijklmnop1234",
        "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789",
        "AKIAIOSFODNN7EXAMPLE",
        "AIzaSyD-" + "abcdefghijklmnopqrstuvwxyz0123456",
        "xoxb-1234567890-abcdefghijklmnop",
        "SG." + "abcdefghijklmnopqrst" + "." + "uvwxyz01234567890abcd",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27u",
    ],
)
def test_known_vendor_tokens_are_redacted(secret: str) -> None:
    source = f'const key = "{secret}";\n'
    result = redact(source)
    assert secret not in result.text
    assert result.redacted_count >= 1


def test_pem_private_key_block_is_redacted_whole() -> None:
    result = redact(f"const key = `{_PEM}`;\n")
    assert "MIIEowIBAAKCAQEA" not in result.text
    assert result.redacted_count == 1


def test_url_password_is_redacted_but_host_survives() -> None:
    source = 'const dsn = "postgresql://depfix:hunter2SuperSecret@db.example.com:5432/app";\n'
    result = redact(source)
    assert "hunter2SuperSecret" not in result.text
    assert "db.example.com" in result.text


def test_named_assignment_redacts_only_the_value() -> None:
    result = redact('const config = { apiKey: "totally-secret-value-here" };\n')
    assert "totally-secret-value-here" not in result.text
    assert "apiKey" in result.text


def test_high_entropy_literal_is_redacted() -> None:
    blob = "Zk3Qm9Xr2Lp7Vt4Nb8Ys1Cd6Wf0Hj5Ag3Ke"
    assert redact(f'const t = "{blob}";\n').redacted_count == 1


def test_ordinary_code_is_left_alone() -> None:
    source = (
        "const client = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });\n"
        'const url = "https://api.openai.com/v1/chat/completions";\n'
        "export async function chat(messages) { return client.chat.completions.create({ messages }); }\n"
    )
    result = redact(source)
    assert result.text == source
    assert result.redacted_count == 0


# -- placeholders ------------------------------------------------------------


def test_identical_secrets_share_one_placeholder() -> None:
    result = redact(f'a = "{_OPENAI}"\nb = "{_OPENAI}"\n')
    assert result.redacted_count == 1
    assert len(PLACEHOLDER_RE.findall(result.text)) == 2


def test_different_secrets_get_different_placeholders() -> None:
    result = redact(f'a = "{_OPENAI}"\nb = "{_STRIPE}"\n')
    assert result.redacted_count == 2
    assert len(set(PLACEHOLDER_RE.findall(result.text))) == 2


def test_placeholder_does_not_contain_the_secret() -> None:
    result = redact(f'a = "{_OPENAI}"\n')
    (token,) = PLACEHOLDER_RE.findall(result.text)
    assert _OPENAI[8:] not in token


# -- restore -----------------------------------------------------------------


def test_restore_round_trips_an_unchanged_file() -> None:
    source = f'const key = "{_OPENAI}";\nexport default key;\n'
    result = redact(source)
    assert result.restore(result.text) == source


def test_restore_round_trips_a_genuinely_edited_file() -> None:
    source = f'const key = "{_OPENAI}";\nopenai.createModeration({{ input }});\n'
    result = redact(source)
    edited = result.text.replace("createModeration", "moderations.create")
    restored = result.restore(edited)
    assert _OPENAI in restored
    assert "moderations.create" in restored


def test_restore_refuses_when_a_placeholder_was_dropped() -> None:
    result = redact(f'const key = "{_OPENAI}";\n')
    with pytest.raises(RedactionError, match="dropped"):
        result.restore("const key = null;\n")


def test_restore_refuses_a_foreign_placeholder() -> None:
    result = redact(f'const key = "{_OPENAI}";\n')
    forged = result.text + '\nconst other = "DEPFIX_REDACTED_deadbeef1234";\n'
    with pytest.raises(RedactionError, match="do not belong"):
        result.restore(forged)


def test_restore_is_identity_when_nothing_was_redacted() -> None:
    result = redact("const x = 1;\n")
    assert result.restore("const x = 2;\n") == "const x = 2;\n"


# -- cover / redact_text -----------------------------------------------------


def test_cover_reuses_the_same_placeholder_for_a_known_secret() -> None:
    result = redact(f'const key = "{_OPENAI}";\n')
    (token,) = PLACEHOLDER_RE.findall(result.text)
    covered = result.cover(f"the call using {_OPENAI} failed")
    assert _OPENAI not in covered
    assert token in covered


def test_redact_text_is_one_way_and_hides_the_secret() -> None:
    assert _STRIPE not in redact_text(f"log line containing {_STRIPE} oops")


# -- redact_file_usage -------------------------------------------------------


def test_redact_file_usage_covers_body_and_usage_lines() -> None:
    line = f'  const client = new OpenAI({{ apiKey: "{_OPENAI}" }});'
    file_usage = FileUsage(
        filepath="src/client.js",
        file_content=f"{line}\nclient.createModeration({{ input }});\n",
        usages=[
            Usage(
                line_number=1,
                column=2,
                line_content=line,
                context_before=[f"// key {_OPENAI}"],
                context_after=["client.createModeration({ input });"],
                match_text=line,
            )
        ],
    )

    safe, redaction = redact_file_usage(file_usage)

    assert _OPENAI not in safe.file_content
    assert _OPENAI not in safe.usages[0].line_content
    assert _OPENAI not in safe.usages[0].context_before[0]
    assert redaction.restore(safe.file_content) == file_usage.file_content


def test_redact_file_usage_returns_the_original_object_when_clean() -> None:
    file_usage = FileUsage(filepath="a.js", file_content="const x = 1;\n", usages=[])
    safe, redaction = redact_file_usage(file_usage)
    assert safe is file_usage
    assert redaction.redacted_count == 0
