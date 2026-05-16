// Murano — local-first knowledge base UI
// Vanilla JS (no framework). ~250 lines.

(() => {
  // ---------- API token (audit-4 finding 2.5) ----------
  // If the server was started with --api-token, the page meta tag carries
  // the token and we attach it to every fetch on /api/.
  const apiTokenMeta = document.querySelector('meta[name="murano-api-token"]');
  const API_TOKEN = apiTokenMeta ? apiTokenMeta.getAttribute("content") : "";

  // Monkey-patch fetch so we don't have to remember to add the header at
  // every call site. Only adds the header to /api/ requests.
  const _origFetch = window.fetch.bind(window);
  window.fetch = function muranoFetch(input, init) {
    init = init || {};
    let url = typeof input === "string" ? input : (input && input.url) || "";
    if (API_TOKEN && url.startsWith("/api/")) {
      const headers = new Headers(init.headers || {});
      if (!headers.has("X-Murano-Token")) headers.set("X-Murano-Token", API_TOKEN);
      init.headers = headers;
    }
    return _origFetch(input, init);
  };

  // ---------- theme toggle ----------
  const root = document.documentElement;
  const savedTheme = localStorage.getItem("murano.theme");
  if (savedTheme === "light" || savedTheme === "dark") {
    root.setAttribute("data-theme", savedTheme);
  }
  document.getElementById("theme-toggle")?.addEventListener("click", () => {
    const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    localStorage.setItem("murano.theme", next);
  });

  // ---------- chat page (only if these elements exist) ----------
  const askForm = document.getElementById("ask-form");
  if (askForm) initChat();

  // ---------- browse page ----------
  const vaultTreeEl = document.getElementById("vault-tree");
  if (vaultTreeEl) initBrowse();

  // ---------- settings page ----------
  const pingBtn = document.getElementById("ping-btn");
  const reindexBtn = document.getElementById("reindex-btn");
  const rebuildBtn = document.getElementById("rebuild-tree-btn");
  if (pingBtn || reindexBtn || rebuildBtn) initSettings();

  // ===================================================================
  // CHAT
  // ===================================================================
  function initChat() {
    const queryEl = document.getElementById("query");
    const askBtn = document.getElementById("ask-btn");
    const cancelBtn = document.getElementById("cancel-btn");
    const answerEl = document.getElementById("answer");
    const sourcesEl = document.getElementById("sources");
    const sourcesListEl = document.getElementById("sources-list");
    const retrievalPanel = document.getElementById("retrieval");
    const metaEl = retrievalPanel.querySelector(".retrieval-meta");
    const themesEl = retrievalPanel.querySelector(".themes-panel");
    const hitsEl = retrievalPanel.querySelector(".hits-panel");
    const statusEl = document.getElementById("status");

    let currentController = null;
    let lastHits = [];
    // Citation tokens (`[[file#heading]]`) often arrive split across multiple
    // SSE deltas (the model streams "[[file" + "#h]]" in separate chunks).
    // We keep a carry buffer of unflushed tail text so we can detect a half-
    // open `[[` and hold its bytes until the matching `]]` arrives.
    // (Audit found that the previous per-delta parser dropped these
    // citations to plain text.)
    let answerCarry = "";

    askForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const query = queryEl.value.trim();
      if (!query) return;
      await runAsk(query);
    });
    queryEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        askForm.requestSubmit();
      }
    });

    cancelBtn.addEventListener("click", () => {
      if (currentController) currentController.abort();
    });

    async function runAsk(query) {
      // Reset UI
      answerEl.textContent = "";
      sourcesListEl.innerHTML = "";
      sourcesEl.hidden = true;
      themesEl.innerHTML = "";
      hitsEl.innerHTML = "";
      retrievalPanel.hidden = true;
      setStatus("Asking…", "");
      askBtn.disabled = true;
      cancelBtn.disabled = false;
      lastHits = [];
      answerCarry = "";

      const body = {
        query,
        k: parseInt(document.getElementById("k").value, 10),
        summary_k: parseInt(document.getElementById("summary_k").value, 10),
        max_tokens: parseInt(document.getElementById("max_tokens").value, 10),
        temperature: parseFloat(document.getElementById("temperature").value),
      };

      currentController = new AbortController();
      try {
        const resp = await fetch("/api/v1/ask", {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
          body: JSON.stringify(body),
          signal: currentController.signal,
        });
        if (!resp.ok) {
          const text = await resp.text();
          setStatus(`HTTP ${resp.status}: ${text}`, "is-error");
          return;
        }
        await consumeSSE(resp.body, handleEvent);
      } catch (err) {
        if (err.name === "AbortError") {
          setStatus("Cancelled.", "is-warn");
        } else {
          setStatus(`Error: ${err.message}`, "is-error");
        }
      } finally {
        askBtn.disabled = false;
        cancelBtn.disabled = true;
        currentController = null;
      }
    }

    function handleEvent(event, data) {
      if (event === "retrieval") {
        retrievalPanel.hidden = false;
        metaEl.textContent =
          `Retrieved ${data.hits.length} chunks` +
          (data.summaries.length ? ` + ${data.summaries.length} theme(s)` : "") +
          ` in ${Math.round(data.elapsed_ms)} ms`;
        themesEl.innerHTML = "";
        for (const s of data.summaries) {
          const pill = el("div", { class: "theme-pill" });
          pill.append(
            el("div", { class: "title" }, s.title || s.node_id),
            el("div", { class: "summary" }, s.summary || ""),
          );
          themesEl.append(pill);
        }
        hitsEl.innerHTML = "";
        for (const h of data.hits) {
          const chip = el("div", { class: "hit-chip" },
            el("span", { class: "num" }, `${h.rank}.`),
            el("span", {}, h.file_path + (h.heading_path ? ` › ${h.heading_path}` : "")),
          );
          hitsEl.append(chip);
        }
        lastHits = data.hits;
        setStatus("Generating…", "");
      } else if (event === "delta") {
        appendAnswerText(data.text);
      } else if (event === "done") {
        // Flush any unfinished tail (e.g. the model produced trailing `[[`
        // and the stream ended; render it as plain text rather than swallow it).
        if (answerCarry) {
          answerEl.append(document.createTextNode(answerCarry));
          answerCarry = "";
        }
        renderSources(lastHits, data.cited || []);
        setStatus(`Done — ${data.finish_reason || "complete"}.`, "is-ok");
      } else if (event === "error") {
        setStatus(`Error: ${data.text}`, "is-error");
      }
    }

    function appendAnswerText(piece) {
      // Append to the carry buffer; then peel off the longest prefix that
      // contains zero in-flight `[[` openings. Render that prefix; keep the
      // rest of the buffer (any half-open `[[...`) for the next delta.
      let buf = answerCarry + piece;
      const re = /\[\[([^\[\]]+?)\]\]/g;
      let lastEnd = 0;
      let renderedUpTo = 0;
      let m;
      while ((m = re.exec(buf)) !== null) {
        // Plain text between the previous citation (or start) and this one.
        if (m.index > lastEnd) {
          answerEl.append(document.createTextNode(buf.slice(lastEnd, m.index)));
        }
        answerEl.append(makeCitation(m[1]));
        lastEnd = m.index + m[0].length;
        renderedUpTo = lastEnd;
      }
      // Anything after the last complete citation MIGHT be the start of
      // another citation that's split across deltas. If we can prove it
      // contains no open `[[`, render it now; otherwise hold it in the carry.
      const tail = buf.slice(renderedUpTo);
      const lastOpen = tail.lastIndexOf("[[");
      if (lastOpen === -1) {
        // No half-open token in the tail — safe to flush everything.
        if (tail) answerEl.append(document.createTextNode(tail));
        answerCarry = "";
      } else {
        // Render up to (but not including) the half-open `[[`, hold the rest.
        if (lastOpen > 0) {
          answerEl.append(document.createTextNode(tail.slice(0, lastOpen)));
        }
        answerCarry = tail.slice(lastOpen);
      }
    }

    function makeCitation(key) {
      // key looks like "cooking/risotto#Method"; the file part is everything before #
      const hashIdx = key.indexOf("#");
      const filePart = hashIdx >= 0 ? key.slice(0, hashIdx) : key;
      const guesses = [filePart + ".md", filePart + ".markdown"];
      const a = el("a", { class: "citation", href: "#", title: "Click to open in your editor" }, `[[${key}]]`);
      a.addEventListener("click", async (ev) => {
        ev.preventDefault();
        for (const path of guesses) {
          try {
            const r = await fetch("/api/v1/open", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ path }),
            });
            if (r.ok) return;
          } catch { /* fall through */ }
        }
        setStatus(`Could not open file for ${key}`, "is-warn");
      });
      return a;
    }

    function renderSources(hits, cited) {
      sourcesEl.hidden = false;
      sourcesListEl.innerHTML = "";
      const citedSet = new Set(cited);
      for (const h of hits) {
        const li = el("li");
        const isCited = citedSet.has(h.citation_key);
        li.append(
          el("span", { class: isCited ? "cited-mark" : "uncited-mark" }, isCited ? "✓" : "·"),
          el("span", {}, h.file_path),
          h.heading_path ? el("span", { class: "muted" }, ` — ${h.heading_path}`) : "",
          el("code", {}, `[[${h.citation_key}]]`),
        );
        sourcesListEl.append(li);
      }
    }

    function setStatus(text, cls) {
      statusEl.className = "status " + (cls || "");
      statusEl.textContent = text;
    }
  }

  // ===================================================================
  // BROWSE
  // ===================================================================
  function initBrowse() {
    const treeEl = document.getElementById("vault-tree");
    const emptyEl = document.getElementById("file-empty");
    const viewEl = document.getElementById("file-view");
    const pathEl = document.getElementById("file-path");
    const contentEl = document.getElementById("file-content");
    const openBtn = document.getElementById("open-in-editor");
    const captureForm = document.getElementById("capture-form");
    const captureInput = document.getElementById("capture-url");
    const captureStatus = document.getElementById("capture-status");

    let activePath = null;

    fetch("/api/v1/vault/tree")
      .then((r) => r.json())
      .then((data) => {
        treeEl.innerHTML = "";
        if (!data.entries.length) {
          treeEl.append(el("p", { class: "muted" }, "Vault is empty."));
          return;
        }
        treeEl.append(renderTree(data.entries));
      });

    function renderTree(entries) {
      const root = el("ul", { class: "tree-list" });
      for (const e of entries) {
        const li = el("li", { class: "tree-node " + (e.type === "dir" ? "tree-dir" : "") });
        if (e.type === "dir") {
          li.append(el("div", { class: "tree-label" }, e.name + "/"));
          li.append(renderTree(e.children));
        } else {
          const a = el("a", { class: "tree-file", href: "#" }, e.name);
          a.dataset.path = e.path;
          a.addEventListener("click", (ev) => {
            ev.preventDefault();
            for (const f of treeEl.querySelectorAll(".tree-file.active")) f.classList.remove("active");
            a.classList.add("active");
            loadFile(e.path);
          });
          li.append(a);
        }
        root.append(li);
      }
      return root;
    }

    async function loadFile(path) {
      activePath = path;
      const resp = await fetch("/api/v1/vault/file?path=" + encodeURIComponent(path));
      if (!resp.ok) {
        contentEl.textContent = `(error loading ${path})`;
        return;
      }
      const data = await resp.json();
      emptyEl.hidden = true;
      viewEl.hidden = false;
      pathEl.textContent = path;
      contentEl.textContent = data.content;
    }

    openBtn.addEventListener("click", async () => {
      if (!activePath) return;
      const r = await fetch("/api/v1/open", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: activePath }),
      });
      if (!r.ok) alert(await r.text());
    });

    captureForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const url = captureInput.value.trim();
      if (!url) return;
      captureStatus.className = "status";
      captureStatus.textContent = `Capturing ${url}…`;
      try {
        const r = await fetch("/api/v1/capture", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url }),
        });
        if (!r.ok) {
          captureStatus.className = "status is-error";
          captureStatus.textContent = `HTTP ${r.status}: ${await r.text()}`;
          return;
        }
        const data = await r.json();
        captureStatus.className = "status is-ok";
        captureStatus.textContent = `Captured ${data.relpath} (${data.word_count} words, ${data.chunks_indexed} chunks)`;
        captureInput.value = "";
        // Refresh the tree
        const treeResp = await fetch("/api/v1/vault/tree");
        const treeData = await treeResp.json();
        treeEl.innerHTML = "";
        treeEl.append(renderTree(treeData.entries));
      } catch (err) {
        captureStatus.className = "status is-error";
        captureStatus.textContent = `Error: ${err.message}`;
      }
    });
  }

  // ===================================================================
  // SETTINGS
  // ===================================================================
  function initSettings() {
    document.getElementById("ping-btn")?.addEventListener("click", async () => {
      const out = document.getElementById("ping-result");
      out.className = "status";
      out.textContent = "Pinging Venice…";
      try {
        const r = await fetch("/api/v1/ping", { method: "POST" });
        const data = await r.json();
        if (!r.ok) {
          out.className = "status is-error";
          out.textContent = `HTTP ${r.status}: ${data.detail || ""}`;
          return;
        }
        out.className = "status is-ok";
        out.textContent = `OK — chat=${data.chat.resolved}, embed=${data.embed.resolved} (${data.embed.embedding_dimensions || "?"}d)`;
      } catch (err) {
        out.className = "status is-error";
        out.textContent = `Error: ${err.message}`;
      }
    });

    document.getElementById("reindex-btn")?.addEventListener("click", async () => {
      const out = document.getElementById("reindex-result");
      out.className = "status";
      out.textContent = "Re-indexing vault…";
      try {
        const r = await fetch("/api/v1/index", { method: "POST" });
        const data = await r.json();
        if (!r.ok) {
          out.className = "status is-error";
          out.textContent = `HTTP ${r.status}: ${data.detail || ""}`;
          return;
        }
        out.className = "status is-ok";
        out.textContent =
          `Indexed ${data.files_indexed} new/changed, ${data.files_unchanged} unchanged, ` +
          `${data.chunks_inserted} chunks in ${data.elapsed_seconds.toFixed(1)}s`;
      } catch (err) {
        out.className = "status is-error";
        out.textContent = `Error: ${err.message}`;
      }
    });

    document.getElementById("rebuild-tree-btn")?.addEventListener("click", async () => {
      const out = document.getElementById("rebuild-tree-result");
      out.className = "status";
      out.textContent = "Rebuilding tree (this can take minutes)…";
      try {
        const r = await fetch("/api/v1/tree/rebuild", { method: "POST" });
        const data = await r.json();
        if (!r.ok) {
          out.className = "status is-error";
          out.textContent = `HTTP ${r.status}: ${data.detail || ""}`;
          return;
        }
        out.className = "status is-ok";
        out.textContent =
          `${data.total_nodes} nodes across ${data.levels.length} level(s) in ${data.elapsed_seconds.toFixed(1)}s`;
      } catch (err) {
        out.className = "status is-error";
        out.textContent = `Error: ${err.message}`;
      }
    });
  }

  // ===================================================================
  // helpers
  // ===================================================================
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (k === "class") node.className = v;
        else if (k === "dataset") Object.assign(node.dataset, v);
        else node.setAttribute(k, v);
      }
    }
    for (const c of children.flat()) {
      if (c == null || c === false) continue;
      if (typeof c === "string") node.append(document.createTextNode(c));
      else node.append(c);
    }
    return node;
  }

  async function consumeSSE(stream, handler) {
    const reader = stream.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const raw = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const ev = parseEvent(raw);
        if (ev) handler(ev.event, ev.data);
      }
    }
  }

  function parseEvent(raw) {
    let event = "message";
    const dataLines = [];
    for (const line of raw.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    }
    if (!dataLines.length) return null;
    let data;
    const dataStr = dataLines.join("\n");
    try { data = JSON.parse(dataStr); }
    catch { data = dataStr; }
    return { event, data };
  }
})();
