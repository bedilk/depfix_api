from argparse import Namespace

from depfix.defaults import fill_pipeline_context


def test_pipeline_context_does_not_restore_database_specific_change_inputs() -> None:
    """A remembered event ID can be invalid after switching databases."""
    args = Namespace(
        repos=["bedilk/seed-stripe-apiversion"],
        repo=None,
        provider=["stripe"],
        from_event=None,
        change_file=None,
    )

    fill_pipeline_context(
        args,
        {
            "last_repos": ["bedilk/other-repo"],
            "last_provider": "openai",
            "last_from_event": 2,
            "last_change_file": "changes/stale.json",
        },
    )

    assert args.from_event is None
    assert args.change_file is None
