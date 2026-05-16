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
    from .capture.web import CaptureError, capture_url

    settings = load_settings()
    if not settings.vault_root.exists():
        err_console.print(
            f"Vault does not exist at [bold]{settings.vault_root}[/]. "
            "Run [bold]murano init[/] first."
        )
        raise typer.Exit(code=1)

    console.print(f"[dim]Fetching {url} ...[/]")
    try:
        page = capture_url(settings, url, extra_tags=tags or None)
    except CaptureError as e:
        err_console.print(str(e))
        raise typer.Exit(code=4) from e

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

    if no_index:
        console.print(
            "[dim]Skipped auto-indexing. Run [bold]murano index[/] or [bold]murano watch[/] "
            "to embed it.[/]"
        )
        return

    from .index.indexer import index_vault

    console.print("[dim]Indexing the new file ...[/]")
    try:
        report = index_vault(settings, subpath=Path(page.relpath))
    except VeniceAuthError as e:
        err_console.print(str(e))
        raise typer.Exit(code=2) from e
    except VeniceConnectionError as e:
        err_console.print(str(e))
        raise typer.Exit(code=3) from e
    console.print(
        f"[green]Indexed[/] {page.relpath} \u2014 "
        f"{report.chunks_inserted} chunks ({report.elapsed_seconds:.2f}s)."
    )


@app.command()
def serve(
    restart: bool = typer.Option(  # noqa: ARG001
        False,
        "--restart",
        help="Kill any prior process on the configured port before starting.",
    ),
) -> None:
    """Run the local web UI + REST API on http://localhost:3000. (Phase 6)"""
    _not_yet("Phase 6", "serve")


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

    Exposes `search_kb` and `ask_kb` as MCP tools. Wire into Claude Desktop,
    Cursor, Hermes, OpenClaw, etc. via the configs in `integrations/`.

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
