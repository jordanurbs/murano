"""Murano CLI — `typer` entrypoint."""

from __future__ import annotations

from getpass import getpass
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import (
    KEYRING_SERVICE,
    KEYRING_USERNAME,
    delete_api_key,
    ensure_dirs,
    get_api_key,
    load_settings,
    save_settings,
)
from .venice import (
    VeniceAuthError,
    VeniceConnectionError,
    list_all_model_ids,
    resolve_models,
)

app = typer.Typer(
    name="murano",
    help="Private, local-first personal knowledge base — chat with your Markdown vault via Venice.",
    no_args_is_help=True,
    add_completion=False,
)

config_app = typer.Typer(
    name="config",
    help="Manage Murano configuration and the Venice API key.",
    no_args_is_help=True,
)
app.add_typer(config_app, name="config")

tree_app = typer.Typer(
    name="tree",
    help="Manage the hierarchical summary tree (the \"memory tree\").",
    no_args_is_help=True,
)
app.add_typer(tree_app, name="tree")

console = Console()
err_console = Console(stderr=True, style="bold red")


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"murano {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show Murano version and exit.",
    ),
) -> None:
    """Murano root command. Use `murano --help` to list subcommands."""


@app.command()
def init() -> None:
    """Create the canonical vault and the derived data directory.

    Creates ~/murano/vault/ and ~/.murano/{logs/, config.toml}. Idempotent.
    """
    settings = load_settings()
    created = ensure_dirs(settings)
    save_settings(settings)

    table = Table(title="Murano paths", show_header=True, header_style="bold cyan")
    table.add_column("Label")
    table.add_column("Path")
    table.add_column("Status")
    for label, path in [
        ("vault", settings.vault_root),
        ("data", settings.data_root),
        ("logs", settings.logs_dir),
        ("config", settings.config_path),
    ]:
        if label == "config":
            status = "written" if settings.config_path.exists() else "missing"
        else:
            status = "created" if label in created else "exists"
        table.add_row(label, str(path), status)
    console.print(table)

    if not get_api_key():
        console.print(
            "\n[yellow]Next:[/] run [bold]murano config set-key[/] to store your Venice API key,"
            " then [bold]murano ping[/] to verify."
        )
    else:
        console.print("\n[green]API key already present in the OS keychain.[/]")


@config_app.command("set-key")
def config_set_key(
    key: str = typer.Option(
        None,
        "--key",
        help="Venice API key. If omitted, you will be prompted (input is hidden).",
    ),
) -> None:
    """Store the Venice API key in the OS keychain."""
    from . import config as cfg

    if not key:
        try:
            key = getpass("Venice API key: ").strip()
        except (KeyboardInterrupt, EOFError):
            err_console.print("Aborted.")
            raise typer.Exit(code=1) from None

    if not key:
        err_console.print("No key provided.")
        raise typer.Exit(code=1)

    cfg.set_api_key(key)
    console.print(
        f"[green]Stored Venice API key[/] in OS keychain "
        f"([dim]service={KEYRING_SERVICE}, username={KEYRING_USERNAME}[/])."
    )


@config_app.command("unset-key")
def config_unset_key() -> None:
    """Remove the Venice API key from the OS keychain."""
    delete_api_key()
    console.print("[green]Venice API key removed from the OS keychain.[/]")


@config_app.command("show")
def config_show() -> None:
    """Print the current resolved configuration."""
    settings = load_settings()
    key_present = bool(get_api_key())

    table = Table(title="Murano configuration", show_header=True, header_style="bold cyan")
    table.add_column("Setting")
    table.add_column("Value")
    table.add_row("vault_root", str(settings.vault_root))
    table.add_row("data_root", str(settings.data_root))
    table.add_row("config_path", str(settings.config_path))
    table.add_row("chunks_db", str(settings.chunks_db))
    table.add_row("summary_tree_db", str(settings.summary_tree_db))
    table.add_row("logs_dir", str(settings.logs_dir))
    table.add_row("venice_base_url", settings.venice_base_url)
    table.add_row("chat_model", settings.chat_model)
    table.add_row("embed_model", settings.embed_model)
    table.add_row("web_port", str(settings.web_port))
    table.add_row(
        "venice_api_key",
        "[green]stored in keychain[/]" if key_present else "[red]not set[/]",
    )
    console.print(table)


@app.command()
def models() -> None:
    """List every model ID Venice advertises across all types."""
    settings = load_settings()
    try:
        ids = list_all_model_ids(settings)
    except VeniceAuthError as e:
        err_console.print(str(e))
        raise typer.Exit(code=2) from e
    except VeniceConnectionError as e:
        err_console.print(str(e))
        raise typer.Exit(code=3) from e

    table = Table(title=f"Venice models ({len(ids)})", show_header=False)
    table.add_column("id")
    for m in sorted(ids):
        table.add_row(m)
    console.print(table)


def _print_resolution_note(role: str, model) -> None:  # noqa: ANN001
    if model.match == "exact":
        return
    if model.match == "prefix":
        console.print(
            f"  [yellow]note[/] configured {role} model "
            f"[dim]{model.requested}[/] is not an exact match; "
            f"using closest available [bold]{model.resolved}[/]."
        )
    else:
        console.print(
            f"  [red]warn[/] configured {role} model "
            f"[dim]{model.requested}[/] was not found in Venice's catalog. "
            f"Run [bold]murano models[/] to list available IDs."
        )


@app.command()
def ping() -> None:
    """Validate Venice connectivity and resolve configured model IDs against /v1/models.

    Acceptance (per MURANO_PLAN.md §11 Phase 1): prints
    `Venice OK, chat=<chat-id>, embed=<embed-id>`.
    """
    settings = load_settings()
    try:
        resolved = resolve_models(settings)
    except VeniceAuthError as e:
        err_console.print(str(e))
        raise typer.Exit(code=2) from e
    except VeniceConnectionError as e:
        err_console.print(str(e))
        raise typer.Exit(code=3) from e

    console.print(
        f"[green]Venice OK[/], chat=[bold]{resolved.chat.resolved}[/], "
        f"embed=[bold]{resolved.embed.resolved}[/]"
    )

    if resolved.embed.embedding_dimensions or resolved.embed.max_input_tokens:
        details = []
        if resolved.embed.embedding_dimensions:
            details.append(f"{resolved.embed.embedding_dimensions} dims")
        if resolved.embed.max_input_tokens:
            details.append(f"max {resolved.embed.max_input_tokens} tokens")
        console.print(f"  [dim]embed: {', '.join(details)}[/]")

    _print_resolution_note("chat", resolved.chat)
    _print_resolution_note("embed", resolved.embed)


def _not_yet(phase: str, what: str) -> None:
    err_console.print(f"`murano {what}` lands in {phase}. Not implemented yet.")
    raise typer.Exit(code=64)


def _print_index_report(report) -> None:  # noqa: ANN001
    summary = Table(title="Index summary", show_header=False, header_style="bold cyan")
    summary.add_column("metric", style="dim")
    summary.add_column("value")
    summary.add_row("vault files seen", str(report.files_seen))
    summary.add_row("indexed (new/changed)", str(report.files_indexed))
    summary.add_row("unchanged (skipped)", str(report.files_unchanged))
    summary.add_row("removed from vault", str(report.files_removed))
    summary.add_row("chunks inserted", str(report.chunks_inserted))
    summary.add_row("embed model", str(report.embed_model))
    summary.add_row("embed dims", str(report.embed_dims))
    summary.add_row("elapsed", f"{report.elapsed_seconds:.2f}s")
    console.print(summary)

    if report.errors:
        err_table = Table(title=f"Errors ({len(report.errors)})", header_style="bold red")
        err_table.add_column("file")
        err_table.add_column("error")
        for e in report.errors:
            err_table.add_row(e.relpath, e.error or "")
        err_console.print(err_table)


@app.command()
def index(
    path: str | None = typer.Option(
        None,
        "--path",
        help="Vault-relative subdirectory or file to index (default: entire vault).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-embed every file even if its content hash is unchanged.",
    ),
) -> None:
    """Index the vault into chunks.db (idempotent via content hash)."""
    from .index.indexer import index_vault

    settings = load_settings()
    if not settings.vault_root.exists():
        err_console.print(
            f"Vault does not exist at [bold]{settings.vault_root}[/]. "
            "Run [bold]murano init[/] first."
        )
        raise typer.Exit(code=1)

    sub = Path(path) if path else None
    try:
        report = index_vault(settings, subpath=sub, force=force)
    except VeniceAuthError as e:
        err_console.print(str(e))
        raise typer.Exit(code=2) from e
    except VeniceConnectionError as e:
        err_console.print(str(e))
        raise typer.Exit(code=3) from e

    _print_index_report(report)


@app.command()
def reindex() -> None:
    """Wipe chunks.db and rebuild it from scratch."""
    from .index.indexer import reindex_vault

    settings = load_settings()
    if not settings.vault_root.exists():
        err_console.print(
            f"Vault does not exist at [bold]{settings.vault_root}[/]. "
            "Run [bold]murano init[/] first."
        )
        raise typer.Exit(code=1)

    try:
        report = reindex_vault(settings)
    except VeniceAuthError as e:
        err_console.print(str(e))
        raise typer.Exit(code=2) from e
    except VeniceConnectionError as e:
        err_console.print(str(e))
        raise typer.Exit(code=3) from e

    _print_index_report(report)


@app.command()
def watch() -> None:
    """Watch the vault and re-index changed Markdown files in real time."""
    from .vault.watcher import watch_vault

    settings = load_settings()
    if not settings.vault_root.exists():
        err_console.print(
            f"Vault does not exist at [bold]{settings.vault_root}[/]. "
            "Run [bold]murano init[/] first."
        )
        raise typer.Exit(code=1)

    console.print(
        f"[green]Watching[/] [bold]{settings.vault_root}[/] for changes. "
        "[dim](Ctrl-C to stop)[/]"
    )

    def on_batch(paths, report):  # noqa: ANN001
        for r in report.errors:
            err_console.print(f"  [red]error[/] {r.relpath}: {r.error}")
        for path in sorted(paths):
            verdict = "no change"
            if report.files_indexed:
                verdict = f"indexed ({report.chunks_inserted} chunks)"
            elif report.files_removed:
                verdict = "removed"
            console.print(f"  [cyan]\u2022[/] {path} \u2014 {verdict}")

    try:
        watch_vault(settings, on_batch=on_batch)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped watching.[/]")
    except VeniceAuthError as e:
        err_console.print(str(e))
        raise typer.Exit(code=2) from e


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query."),
    k: int = typer.Option(10, "-k", "--k", help="How many top hits to return."),
) -> None:
    """Vector search: print the top-K matching chunks (no LLM call)."""
    from .index.search import search as do_search

    settings = load_settings()
    try:
        hits = do_search(settings, query, k=k)
    except VeniceAuthError as e:
        err_console.print(str(e))
        raise typer.Exit(code=2) from e
    except VeniceConnectionError as e:
        err_console.print(str(e))
        raise typer.Exit(code=3) from e

    if not hits:
        console.print("[yellow]No matches.[/] Try [bold]murano index[/] first.")
        return

    for i, hit in enumerate(hits, start=1):
        header = (
            f"[bold cyan]{i}.[/] [bold]{hit.file_path}[/]"
            + (f" [dim]\u2014 {hit.heading_path}[/]" if hit.heading_path else "")
            + f"  [dim]distance={hit.distance:.4f}, tokens={hit.token_count}[/]"
        )
        console.print(header)
        preview = hit.content.strip()
        if len(preview) > 320:
            preview = preview[:320].rstrip() + "\u2026"
        console.print(f"  {preview}\n")


@app.command()
def ask(
    query: str = typer.Argument(..., help="Question to ask the knowledge base."),
    k: int = typer.Option(6, "-k", "--k", help="How many chunks to retrieve as context."),
    max_tokens: int = typer.Option(
        1024, "--max-tokens", help="Maximum answer length in tokens."
    ),
    temperature: float = typer.Option(
        0.2, "--temperature", help="Sampling temperature for the chat model."
    ),
    show_context: bool = typer.Option(
        False,
        "--show-context",
        help="Print the retrieved chunks before the answer (useful for debugging).",
    ),
) -> None:
    """Ask a question. Streams a cited answer grounded in your vault."""
    from .chat.answer import (
        StreamConfig,
        extract_citation_keys,
        stream_answer,
    )

    settings = load_settings()
    if not settings.chunks_db.exists():
        err_console.print(
            f"No index found at [bold]{settings.chunks_db}[/]. "
            "Run [bold]murano index[/] first."
        )
        raise typer.Exit(code=1)

    cfg = StreamConfig(k=k, max_tokens=max_tokens, temperature=temperature)

    retrieval = None
    answer_chars: list[str] = []
    saw_error = False

    try:
        events = stream_answer(settings, query, config=cfg)
        for ev in events:
            if ev.kind == "retrieval":
                retrieval = ev.retrieval
                hit_count = len(retrieval.hits) if retrieval else 0
                summary_count = len(retrieval.summaries) if retrieval else 0
                summary_str = (
                    f" + {summary_count} theme(s)" if summary_count else ""
                )
                console.print(
                    f"[dim]Retrieved {hit_count} chunks{summary_str} in "
                    f"{retrieval.elapsed_ms:.0f} ms "
                    f"(embed={retrieval.embed_model}, chat={retrieval.chat_model})[/]"
                )
                if show_context and retrieval:
                    if retrieval.summaries:
                        console.print("  [bold]Themes:[/]")
                        for s in retrieval.summaries:
                            console.print(
                                f"    [magenta]\u2022[/] {s.title} "
                                f"[dim]({s.member_count} notes, distance={s.distance:.4f})[/]"
                            )
                    if retrieval.hits:
                        console.print("  [bold]Chunks:[/]")
                        for i, h in enumerate(retrieval.hits, start=1):
                            console.print(
                                f"    [cyan]{i}.[/] {h.file_path}"
                                + (f" [dim]\u2014 {h.heading_path}[/]" if h.heading_path else "")
                                + f"  [dim]distance={h.distance:.4f}[/]"
                            )
                console.print()
            elif ev.kind == "delta" and ev.text:
                answer_chars.append(ev.text)
                console.file.write(ev.text)
                console.file.flush()
            elif ev.kind == "error":
                saw_error = True
                console.print()
                err_console.print(ev.text or "stream error")
                break
    except VeniceAuthError as e:
        err_console.print(str(e))
        raise typer.Exit(code=2) from e
    except VeniceConnectionError as e:
        err_console.print(str(e))
        raise typer.Exit(code=3) from e

    if saw_error:
        raise typer.Exit(code=3)

    console.print()  # finish the answer line

    if retrieval and retrieval.hits:
        from rich.markup import escape

        answer_text = "".join(answer_chars)
        cited = extract_citation_keys(answer_text)
        console.print()
        console.print("[bold]Sources[/]")
        for i, h in enumerate(retrieval.hits, start=1):
            mark = "[green]\u2713[/]" if h.citation_key in cited else "[dim] [/]"
            heading = f" [dim]\u2014 {escape(h.heading_path)}[/]" if h.heading_path else ""
            key_display = escape(f"[[{h.citation_key}]]")
            console.print(
                f"  {mark} \\[{i}] [bold]{escape(h.file_path)}[/]{heading}  "
                f"[dim]({key_display})[/]"
            )
        if not cited:
            console.print(
                "  [yellow]note[/] the model did not emit any inline citations; "
                "the chunks above were the retrieval set."
            )


@app.command()
def capture(
    url: str = typer.Argument(..., help="URL to capture into the vault."),
    tags: list[str] = typer.Option(  # noqa: B008
        [],
        "--tag",
        help="Extra tag to add to the file's frontmatter. Repeatable.",
    ),
    no_index: bool = typer.Option(
        False,
        "--no-index",
        help="Skip auto-indexing the new file; rely on `murano watch` or a manual `murano index`.",
    ),
) -> None:
    """Capture a web page into the vault as a Markdown file with YAML frontmatter."""
    from .capture.web import CaptureError, capture_and_index, capture_url

    settings = load_settings()
    if not settings.vault_root.exists():
        err_console.print(
            f"Vault does not exist at [bold]{settings.vault_root}[/]. "
            "Run [bold]murano init[/] first."
        )
        raise typer.Exit(code=1)

    console.print(f"[dim]Fetching {url} ...[/]")

    if no_index:
        try:
            page = capture_url(settings, url, extra_tags=tags or None)
        except CaptureError as e:
            err_console.print(str(e))
            raise typer.Exit(code=4) from e
        _print_capture_table(page)
        console.print(
            "[dim]Skipped auto-indexing. Run [bold]murano index[/] or "
            "[bold]murano watch[/] to embed it.[/]"
        )
        return

    try:
        result = capture_and_index(settings, url, extra_tags=tags or None)
    except CaptureError as e:
        err_console.print(str(e))
        raise typer.Exit(code=4) from e

    _print_capture_table(result.page)
    if result.chunks_indexed < 0:
        err_console.print(
            f"[yellow]Captured but not indexed:[/] {result.index_skipped_reason}"
        )
        raise typer.Exit(code=3)
    console.print(
        f"[green]Indexed[/] {result.page.relpath} \u2014 "
        f"{result.chunks_indexed} chunks."
    )


def _print_capture_table(page) -> None:  # noqa: ANN001
    table = Table(title="Captured", show_header=False, header_style="bold cyan")
    table.add_column("field", style="dim")
    table.add_column("value")
    table.add_row("title", page.title)
    table.add_row("path", page.relpath)
    table.add_row("words", str(page.word_count))
    table.add_row("size", f"{page.byte_count:,} bytes")
    if page.site_name:
        table.add_row("site", page.site_name)
    if page.published_date:
        table.add_row("published", page.published_date)
    console.print(table)


@app.command("capture-feed")
def capture_feed_cmd(
    feed_url: str = typer.Argument(..., help="Absolute URL of an RSS or Atom feed."),
    limit: int = typer.Option(
        20, "--limit", help="Maximum number of entries to ingest in one run."
    ),
    tags: list[str] = typer.Option(  # noqa: B008
        [],
        "--tag",
        help="Extra tag added to each captured file's frontmatter (alongside `web-capture` and `rss`).",
    ),
) -> None:
    """Capture every new entry in an RSS/Atom feed into the vault.

    State is tracked at ~/.murano/logs/feeds.json so rerunning the same feed
    only fetches new entries.
    """
    from .capture.feed import FeedError, capture_feed

    settings = load_settings()
    if not settings.vault_root.exists():
        err_console.print(
            f"Vault does not exist at [bold]{settings.vault_root}[/]. "
            "Run [bold]murano init[/] first."
        )
        raise typer.Exit(code=1)

    console.print(f"[dim]Fetching feed {feed_url}…[/]")
    try:
        report = capture_feed(settings, feed_url, limit=limit, extra_tags=tags or None)
    except FeedError as e:
        err_console.print(str(e))
        raise typer.Exit(code=4) from e

    table = Table(title=f"Feed: {report.feed_title}", show_header=False, header_style="bold cyan")
    table.add_column("metric", style="dim")
    table.add_column("value")
    table.add_row("entries seen", str(report.entries_total))
    table.add_row("newly captured", str(len(report.captured)))
    table.add_row("already seen (skipped)", str(len(report.seen)))
    table.add_row("errors", str(len(report.errors)))
    console.print(table)

    for r in report.captured:
        console.print(f"  [green]+[/] {r.title}  [dim]{r.relpath}[/]")
    for r in report.errors:
        err_console.print(f"  [red]![/] {r.url} — {r.error}")


@app.command()
def serve(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Address to bind. Defaults to localhost only — change to 0.0.0.0 to expose on the LAN.",
    ),
    port: int | None = typer.Option(
        None,
        "--port",
        help="Port to bind. Defaults to the configured web_port (3000).",
    ),
    restart: bool = typer.Option(
        False,
        "--restart",
        help="Kill any prior process bound to the port + any prior `murano serve` before starting.",
    ),
    schedule: bool = typer.Option(
        True,
        "--schedule/--no-schedule",
        help="Run the nightly tree rebuild scheduler.",
    ),
    watch: bool = typer.Option(
        True,
        "--watch/--no-watch",
        help="Run the vault file watcher in a background thread so dropped notes auto-index.",
    ),
    reload: bool = typer.Option(
        False,
        "--reload",
        help="Hot-reload on source changes (dev only).",
    ),
    api_token: str | None = typer.Option(
        None,
        "--api-token",
        help=(
            "If set, gates every POST/PUT/PATCH/DELETE under /api/ behind a "
            "matching X-Murano-Token header. The bundled UI is given the "
            "token automatically. Required for safe LAN binding."
        ),
    ),
) -> None:
    """Run the local web UI + REST API on http://localhost:3000."""
    import os as _os

    import uvicorn

    from .api.scheduler import kill_port

    settings = load_settings()
    bind_port = port if port is not None else settings.web_port

    # If --api-token is provided, propagate it into the env so the reload
    # subprocess (and the UI route's lazy lookup) sees it.
    if api_token:
        _os.environ["MURANO_API_TOKEN"] = api_token

    if restart:
        killed = kill_port(bind_port)
        console.print(f"[dim]Killed {killed} process(es) on port {bind_port}.[/]")

    console.print(
        f"[green]Starting Murano[/] on [bold]http://{host}:{bind_port}[/]\n"
        f"  schedule={'on' if schedule else 'off'}  "
        f"watch={'on' if watch else 'off'}  "
        f"reload={'on' if reload else 'off'}"
    )

    # Loud warning when the operator binds to anything other than localhost.
    # Mutating endpoints (/open, /capture, /index, /tree/rebuild) can be
    # gated behind --api-token now (audit-4 fix); read endpoints stay open.
    _loopback_hosts = {"127.0.0.1", "::1", "localhost"}
    bind_warning_for_app: str | None = None
    if host not in _loopback_hosts:
        active_token = api_token or _os.environ.get("MURANO_API_TOKEN", "")
        if active_token:
            err_console.print(
                "[bold yellow]NOTE:[/] Murano is binding to a non-loopback "
                f"address ([bold]{host}[/]). Mutating endpoints require the "
                "[bold]X-Murano-Token[/] header (token is set). Read "
                "endpoints (/health, /search, /chunks, /themes) are open."
            )
        else:
            bind_warning_for_app = (
                "Bound to non-loopback address without --api-token. All "
                "endpoints are unauthenticated."
            )
            err_console.print(
                "[bold red]WARNING:[/] Murano is binding to a non-loopback address "
                f"([bold]{host}[/]). All endpoints are unauthenticated. Anyone "
                "able to reach this address can read your vault, capture URLs "
                "from your network (public-internet hosts only), open Markdown "
                "files in your editor, and run LLM calls on your Venice key. "
                "Re-run with [bold]--api-token <secret>[/] to gate the mutating "
                "endpoints, or stop the server and drop the [bold]--host[/] flag."
            )

    if reload:
        # Reload needs the import-string form; background workers off so the
        # reloader's worker processes don't double-up the scheduler.
        import os

        os.environ["MURANO_ENABLE_SCHEDULE"] = "1" if schedule else "0"
        os.environ["MURANO_ENABLE_WATCH"] = "1" if watch else "0"
        uvicorn.run(
            "murano.api._reload_entry:app",
            host=host,
            port=bind_port,
            reload=True,
        )
    else:
        from .api.server import create_app

        application = create_app(
            enable_schedule=schedule,
            enable_watch=watch,
            api_token=api_token,
            bind_warning=bind_warning_for_app,
        )
        uvicorn.run(application, host=host, port=bind_port, log_level="info")


@app.command("licenses")
def licenses_cmd(
    fail_on_copyleft: bool = typer.Option(
        True,
        "--fail-on-copyleft/--no-fail-on-copyleft",
        help="Exit non-zero if any GPL/AGPL/LGPL package is detected (default: yes).",
    ),
    show_all: bool = typer.Option(
        False,
        "--all",
        help="Print every installed package, not just the copyleft ones.",
    ),
) -> None:
    """Audit installed package licenses for copyleft contamination.

    Murano promises 'no GPL/AGPL deps'. This command enforces that.
    Suitable for CI: it exits non-zero when something copyleft slips in.
    """
    from .licenses import audit, copyleft_packages

    pkgs = audit()
    bad = copyleft_packages(pkgs)

    if show_all:
        t = Table(title=f"Installed packages ({len(pkgs)})", header_style="bold cyan")
        t.add_column("package")
        t.add_column("version")
        t.add_column("license")
        t.add_column("flag")
        for p in pkgs:
            flag = (
                f"[red]copyleft[/] ({p.reason})" if p.copyleft else "[green]ok[/]"
            )
            t.add_row(p.name, p.version, p.license or "?", flag)
        console.print(t)

    if not bad:
        console.print(
            f"[green]All clear[/] — {len(pkgs)} packages, none flagged as copyleft."
        )
        return

    err_table = Table(
        title=f"Copyleft packages found ({len(bad)})",
        header_style="bold red",
    )
    err_table.add_column("package")
    err_table.add_column("version")
    err_table.add_column("license")
    err_table.add_column("match")
    for p in bad:
        err_table.add_row(p.name, p.version, p.license or "?", p.reason or "")
    err_console.print(err_table)

    if fail_on_copyleft:
        err_console.print(
            "[red]exit 5[/] — copyleft contamination detected. "
            "Run with [bold]--no-fail-on-copyleft[/] to ignore."
        )
        raise typer.Exit(code=5)


@app.command()
def export(
    out: str | None = typer.Option(
        None,
        "--out",
        help="Output .zip path. Defaults to murano-export-YYYYMMDD-HHMMSS.zip in the current dir.",
    ),
) -> None:
    """Export just the Markdown vault as a portable .zip (Obsidian-compatible)."""
    from .backup import default_export_path, export_vault

    settings = load_settings()
    target = Path(out) if out else default_export_path(settings, "export")
    report = export_vault(settings, target)
    console.print(
        f"[green]Exported[/] {report.file_count} files "
        f"({report.total_bytes:,} bytes) -> [bold]{report.out_path}[/] "
        f"in {report.elapsed_seconds:.2f}s."
    )


@app.command()
def backup(
    out: str | None = typer.Option(
        None,
        "--out",
        help="Output .zip path. Defaults to murano-backup-YYYYMMDD-HHMMSS.zip in the current dir.",
    ),
    include_usage: bool = typer.Option(
        True,
        "--include-usage/--no-include-usage",
        help="Include the token usage log in the backup.",
    ),
) -> None:
    """Backup the vault + config.toml + usage log (no DBs, no API key)."""
    from .backup import backup as do_backup
    from .backup import default_export_path

    settings = load_settings()
    target = Path(out) if out else default_export_path(settings, "backup")
    report = do_backup(settings, target, include_usage=include_usage)
    bits = []
    if report.included_config:
        bits.append("config.toml")
    if report.included_usage_log:
        bits.append("usage.jsonl")
    extras = (" + " + " + ".join(bits)) if bits else ""
    console.print(
        f"[green]Backed up[/] {report.file_count} files{extras} "
        f"({report.total_bytes:,} bytes) -> [bold]{report.out_path}[/] "
        f"in {report.elapsed_seconds:.2f}s."
    )
    console.print(
        "[dim]Excluded by design: chunks.db, summary_tree.db, the Venice API key.[/]"
    )


@app.command()
def usage(
    limit: int = typer.Option(
        20, "--limit", help="How many most-recent events to print in addition to the totals."
    ),
    raw: bool = typer.Option(
        False, "--raw", help="Print the raw usage.jsonl lines instead of a summary."
    ),
) -> None:
    """Summarize Venice token usage logged at ~/.murano/logs/usage.jsonl."""
    from .usage import iter_usage, summarize

    settings = load_settings()
    if raw:
        path = settings.logs_dir / "usage.jsonl"
        if not path.exists():
            err_console.print("[yellow]No usage log yet.[/]")
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            console.print(line)
        return

    events = list(iter_usage(settings.data_root))
    if not events:
        err_console.print("[yellow]No usage logged yet.[/]")
        return

    s = summarize(events)
    totals = Table(title="Totals", show_header=False, header_style="bold cyan")
    totals.add_column("metric", style="dim")
    totals.add_column("value", justify="right")
    totals.add_row("events", f"{s.total_events:,}")
    totals.add_row("prompt tokens", f"{s.total_prompt_tokens:,}")
    totals.add_row("completion tokens", f"{s.total_completion_tokens:,}")
    totals.add_row("total tokens", f"{s.total_tokens:,}")
    console.print(totals)

    def _bucket_table(title: str, buckets: dict[str, dict[str, int]]) -> Table:
        t = Table(title=title, header_style="bold cyan")
        t.add_column("key")
        t.add_column("events", justify="right")
        t.add_column("prompt", justify="right")
        t.add_column("completion", justify="right")
        t.add_column("total", justify="right")
        for k, v in sorted(buckets.items(), key=lambda kv: -kv[1]["total_tokens"]):
            t.add_row(
                k,
                f"{v['events']:,}",
                f"{v['prompt_tokens']:,}",
                f"{v['completion_tokens']:,}",
                f"{v['total_tokens']:,}",
            )
        return t

    console.print(_bucket_table("By operation", s.by_operation))
    console.print(_bucket_table("By model", s.by_model))
    console.print(_bucket_table("By day", s.by_day))

    if limit > 0:
        recent = events[-limit:]
        t = Table(
            title=f"Most recent {len(recent)} events",
            header_style="bold cyan",
        )
        t.add_column("when")
        t.add_column("op")
        t.add_column("model")
        t.add_column("prompt", justify="right")
        t.add_column("completion", justify="right")
        t.add_column("total", justify="right")
        t.add_column("ms", justify="right")
        from datetime import datetime as _dt

        for ev in recent:
            when = (
                _dt.fromtimestamp(ev.timestamp).strftime("%Y-%m-%d %H:%M:%S")
                if ev.timestamp > 0
                else "?"
            )
            t.add_row(
                when,
                ev.operation,
                ev.model,
                f"{ev.prompt_tokens:,}",
                f"{ev.completion_tokens:,}",
                f"{ev.total_tokens:,}",
                f"{ev.elapsed_ms:.0f}" if ev.elapsed_ms is not None else "-",
            )
        console.print(t)


@tree_app.command("rebuild")
def tree_rebuild(
    max_levels: int = typer.Option(
        3, "--max-levels", help="Maximum levels of summary clustering to build."
    ),
    min_cluster_size: int = typer.Option(
        5,
        "--min-cluster-size",
        help="Stop building when fewer than this many items remain at a level.",
    ),
    seed: int = typer.Option(
        0, "--seed", help="Random seed for k-means initialization (for reproducibility)."
    ),
) -> None:
    """(Re)build the summary tree from the current chunks index."""
    from .tree.build import build_tree

    settings = load_settings()
    if not settings.chunks_db.exists():
        err_console.print(
            f"No index found at [bold]{settings.chunks_db}[/]. "
            "Run [bold]murano index[/] first."
        )
        raise typer.Exit(code=1)

    def progress(msg: str) -> None:
        console.print(f"[dim]{msg}[/]")

    try:
        report = build_tree(
            settings,
            max_levels=max_levels,
            min_cluster_size=min_cluster_size,
            seed=seed,
            progress=progress,
        )
    except VeniceAuthError as e:
        err_console.print(str(e))
        raise typer.Exit(code=2) from e
    except VeniceConnectionError as e:
        err_console.print(str(e))
        raise typer.Exit(code=3) from e

    if report.skipped_reason:
        err_console.print(f"[yellow]Tree not built:[/] {report.skipped_reason}")
        raise typer.Exit(code=4)

    table = Table(title="Tree built", show_header=False, header_style="bold cyan")
    table.add_column("metric", style="dim")
    table.add_column("value")
    table.add_row("source chunks", str(report.source_chunk_count))
    table.add_row("total nodes", str(report.total_nodes))
    table.add_row("total edges", str(report.total_edges))
    table.add_row("embed model", str(report.embed_model))
    table.add_row("chat model", str(report.chat_model))
    table.add_row("elapsed", f"{report.elapsed_seconds:.2f}s")
    console.print(table)

    if report.levels:
        levels = Table(title="Per-level stats", header_style="bold cyan")
        levels.add_column("level")
        levels.add_column("inputs")
        levels.add_column("k (clusters)")
        levels.add_column("summary calls")
        levels.add_column("elapsed")
        for s in report.levels:
            levels.add_row(
                str(s.level),
                str(s.inputs),
                str(s.k),
                str(s.summary_calls),
                f"{s.elapsed_seconds:.2f}s",
            )
        console.print(levels)


@tree_app.command("show")
def tree_show(
    level: int | None = typer.Option(
        None, "--level", help="Only print nodes at this level (default: all)."
    ),
) -> None:
    """Print the current summary tree (titles + summaries)."""
    from rich.markup import escape

    from .tree.retrieve import list_themes, status

    settings = load_settings()
    st = status(settings)
    if not st.exists:
        err_console.print("[yellow]No summary tree built yet.[/] Run [bold]murano tree rebuild[/].")
        if st.current_chunk_count == 0:
            err_console.print("  (your index is also empty — start with [bold]murano index[/])")
        raise typer.Exit(code=1)

    header = Table(title="Summary tree", show_header=False, header_style="bold cyan")
    header.add_column("metric", style="dim")
    header.add_column("value")
    header.add_row("nodes", str(st.node_count))
    header.add_row("levels", ", ".join(str(lv) for lv in st.levels))
    header.add_row("source chunks (at build time)", str(st.source_chunk_count))
    header.add_row("current chunks", str(st.current_chunk_count))
    header.add_row("embed model", st.embed_model or "?")
    header.add_row("chat model", st.chat_model or "?")
    if st.is_stale:
        header.add_row("status", f"[yellow]stale[/]: {st.stale_reason}")
    else:
        header.add_row("status", "[green]fresh[/]")
    console.print(header)

    levels_to_print = [level] if level is not None else list(st.levels)
    for lv in levels_to_print:
        nodes = list_themes(settings, level=lv)
        if not nodes:
            console.print(f"\n[dim]Level {lv}: no nodes[/]")
            continue
        console.print(f"\n[bold]Level {lv}[/] ({len(nodes)} nodes)")
        for n in nodes:
            console.print(
                f"  [cyan]{escape(n.id)}[/] [bold]{escape(n.title)}[/]  "
                f"[dim]({n.member_count} members)[/]"
            )
            for line in n.summary.splitlines():
                console.print(f"    [dim]{escape(line)}[/]")


@app.command()
def mcp() -> None:
    """Run the MCP server over stdio for agent frameworks.

    Exposes five MCP tools — `search_kb`, `ask_kb`, `capture_url`,
    `list_themes`, `get_chunk` — backed by the same retrieval/answer core
    the CLI and HTTP API use. Wire into Claude Desktop, Cursor, Hermes,
    OpenClaw, Codex CLI, etc. via the configs in `integrations/`.

    Logs go to stderr; stdout is reserved for the MCP protocol.
    """
    from .mcp.server import main as run_mcp_server

    if not load_settings().chunks_db.exists():
        err_console.print(
            "[yellow]warn[/] no index found yet. Tool calls will return errors "
            "until you run [bold]murano index[/]."
        )
    try:
        run_mcp_server()
    except KeyboardInterrupt:
        err_console.print("\n[yellow]MCP server stopped.[/]")


if __name__ == "__main__":
    app()
