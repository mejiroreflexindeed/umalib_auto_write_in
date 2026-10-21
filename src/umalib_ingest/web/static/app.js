"use strict";

(function () {
  const state = { selectedId: null, items: [], lastDetail: null };
  const writerState = {
    config: null,
    catalog: null,
    catalogLoading: false,
    plan: null,
    preview: null,
    result: null,
    writeBusy: false,
    catalogRequestVersion: 0,
    previewRequestVersion: 0,
    writeRequestVersion: 0,
  };
  let baseline = null;
  let dirty = false;
  let locked = false;
  let pendingAction = null;
  let detailRequestVersion = 0;
  let detailRenderVersion = 0;
  let watcherShaCache = null;

  function $(id) {
    return document.getElementById(id);
  }

  function clear(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function li(text) {
    const item = document.createElement("li");
    item.textContent = text;
    return item;
  }

  function showMessage(node, text, isError) {
    clear(node);
    if (!text) {
      return;
    }
    const span = document.createElement("span");
    span.textContent = text;
    if (isError) {
      span.className = "error-text";
    }
    node.appendChild(span);
  }

  async function api(path, options) {
    const res = await fetch(path, options);
    let body = null;
    try {
      body = await res.json();
    } catch (e) {
      body = null;
    }
    if (body === null || typeof body !== "object" || body.ok !== true) {
      const msg =
        body && body.error && body.error.message ? body.error.message : "请求失败";
      const err = new Error(msg);
      err.code = body && body.error ? body.error.code : "unknown";
      throw err;
    }
    return body.data;
  }

  function jsonOptions(method, payload) {
    return {
      method: method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    };
  }

  function splitLines(text) {
    return text
      .split("\n")
      .filter(function (line) {
        return line.trim() !== "";
      });
  }

  function localDateTimeToUnixSeconds(value) {
    if (typeof value !== "string" || value === "") {
      return NaN;
    }
    const millis = new Date(value).getTime();
    if (typeof millis !== "number" || isNaN(millis)) {
      return NaN;
    }
    return Math.floor(millis / 1000);
  }

  /* ---------------- Inbox list ---------------- */

  async function refreshList() {
    const status = $("status-filter").value;
    const query = status ? "?status=" + encodeURIComponent(status) : "";
    try {
      const data = await api("/api/items" + query);
      state.items = data.items;
      renderItems();
    } catch (err) {
      showMessage($("list-message"), err.message, true);
    }
  }

  function searchText() {
    return $("search-input").value.trim().toLowerCase();
  }

  function applySearch(items) {
    const query = searchText();
    if (query === "") {
      return items;
    }
    return items.filter(function (item) {
      const title = (item.title === null ? "" : item.title).toLowerCase();
      const author = (item.author === null ? "" : item.author).toLowerCase();
      return title.indexOf(query) !== -1 || author.indexOf(query) !== -1;
    });
  }

  function renderItems() {
    const body = $("items-body");
    clear(body);
    const items = applySearch(state.items);
    if (items.length === 0) {
      const row = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 6;
      td.textContent = "（空）";
      row.appendChild(td);
      body.appendChild(row);
      return;
    }
    for (const item of items) {
      const row = document.createElement("tr");
      if (state.selectedId === item.id) {
        row.className = "selected";
      }
      const cells = [
        String(item.id),
        item.status,
        item.title === null ? "" : item.title,
        item.author === null ? "" : item.author,
        item.missing_fields.join(", "),
        item.updated_at,
      ];
      for (const text of cells) {
        const td = document.createElement("td");
        td.textContent = text;
        row.appendChild(td);
      }
      row.addEventListener("click", function () {
        openDetail(item.id);
      });
      body.appendChild(row);
    }
  }

  function openDetail(id) {
    if (writerState.writeBusy) {
      showMessage($('detail-message'), '正在生成候选数据库，请等待当前操作完成。', true);
      return;
    }
    if (guardDirty(function () {
      state.selectedId = id;
      loadDetail(id);
    })) {
      return;
    }
    state.selectedId = id;
    loadDetail(id);
  }

  async function loadDetail(id) {
    const requestVersion = ++detailRequestVersion;
    try {
      const detail = await api("/api/items/" + id);
      if (requestVersion !== detailRequestVersion) {
        return false;
      }
      renderDetail(detail);
      return true;
    } catch (err) {
      if (requestVersion !== detailRequestVersion) {
        return false;
      }
      showMessage($("detail-message"), err.message, true);
      return false;
    }
  }

  function renderList(node, texts, emptyText) {
    clear(node);
    if (texts.length === 0) {
      node.appendChild(li(emptyText));
      return;
    }
    for (const text of texts) {
      node.appendChild(li(text));
    }
  }

  const reviewControlIds = [
    "f-title",
    "f-author",
    "f-translator",
    "f-series",
    "f-tags",
    "f-nga",
    "f-original-series",
    "f-original",
    "f-translation-series",
    "f-translation",
    "f-body",
  ];

  function setLocked(value) {
    locked = value;
    for (const id of reviewControlIds) {
      $(id).disabled = value;
    }
    $("save-btn").disabled = value;
    $("readonly-notice").hidden = !value;
  }

  function renderDetail(d) {
    const renderVersion = ++detailRenderVersion;
    state.selectedId = d.id;
    state.lastDetail = d;
    $("detail-panel").hidden = false;
    $("detail-id").textContent = "#" + String(d.id) + " · " + d.status;
    $("status-selected").textContent = "当前: #" + String(d.id) + " · " + d.status;
    $("f-title").value = d.review_fields.title === null ? "" : d.review_fields.title;
    $("f-author").value = d.review_fields.author === null ? "" : d.review_fields.author;
    $("f-translator").value = d.review_fields.translator === null ? "" : d.review_fields.translator;
    $("f-series").value = d.review_fields.series === null ? "" : d.review_fields.series;
    $("f-tags").value = d.review_fields.tags.join("\n");
    $("f-nga").value = d.review_fields.sources.nga.join("\n");
    $("f-original-series").value = d.review_fields.sources.original_series.join("\n");
    $("f-original").value = d.review_fields.sources.original.join("\n");
    $("f-translation-series").value = d.review_fields.sources.translation_series.join("\n");
    $("f-translation").value = d.review_fields.sources.translation.join("\n");
    $("f-body").value = d.review_fields.body_text;
    $("f-raw").textContent = d.raw_text;
    $("d-missing").textContent =
      d.missing_fields.length === 0
        ? "缺失字段: 无"
        : "缺失字段: " + d.missing_fields.join(", ");
    renderList(
      $("d-warnings"),
      d.warnings.map(function (w) {
        return "[" + w.line + "] " + w.code + " " + w.message +
          (w.field === null ? "" : "（" + w.field + "）");
      }),
      "（无）"
    );
    renderList(
      $("d-unknown"),
      d.unknown_fields.map(function (u) {
        return "[" + u.line + "] " + u.name + " = " + u.value;
      }),
      "（无）"
    );
    const rawFieldTexts = [];
    for (const key of Object.keys(d.raw_fields)) {
      rawFieldTexts.push(key + " → " + d.raw_fields[key].join(" | "));
    }
    renderList($("d-rawfields"), rawFieldTexts, "（无）");
    renderList(
      $("d-duplicates"),
      d.duplicates.map(function (dup) {
        return "#" + dup.id + " · " + dup.status + " · " +
          (dup.title === null ? "" : dup.title);
      }),
      "（无）"
    );
    renderList(
      $("d-history"),
      d.history.map(function (h) {
        return "#" + h.id + " " + (h.from_status === null ? "-" : h.from_status) +
          " → " + h.to_status + "（" + h.reason + "） " + h.changed_at;
      }),
      "（无）"
    );
    setLocked(!d.editable);
    clear($("detail-message"));
    clear($("transition-message"));
    clear($("dirty-message"));
    $("discard-btn").hidden = true;
    $("transition-reason").value = "";
    pendingAction = null;
    renderPackageSection(d, renderVersion);
    resetWriterPanel(d, renderVersion);
    baseline = reviewSnapshot();
    dirty = false;
  }

  function renderPackageSection(d, renderVersion) {
    clear($("pkg-status"));
    clear($("pkg-sha"));
    $("pkg-json").hidden = true;
    clear($("pkg-json"));
    if (d.status !== "approved") {
      $("pkg-status").textContent = "批准后可生成";
      return;
    }
    api("/api/items/" + d.id + "/approved-package")
      .then(function (pkg) {
        if (renderVersion !== detailRenderVersion || state.selectedId !== d.id) {
          return;
        }
        $("pkg-status").textContent = "已生成（只读预览，不会写入大书库）";
        $("pkg-sha").textContent = "package_sha256: " + pkg.package_sha256;
        $("pkg-json").textContent = JSON.stringify(pkg, null, 2);
        $("pkg-json").hidden = false;
      })
      .catch(function (err) {
        if (renderVersion !== detailRenderVersion || state.selectedId !== d.id) {
          return;
        }
        $("pkg-status").textContent = "交接包暂不可生成: " + err.message;
      });
  }

  function renderMutationResult(detail) {
    detailRequestVersion += 1;
    renderDetail(detail);
  }

  function collectReviewFields() {
    return {
      title: $("f-title").value === "" ? null : $("f-title").value,
      author: $("f-author").value === "" ? null : $("f-author").value,
      translator: $("f-translator").value === "" ? null : $("f-translator").value,
      series: $("f-series").value === "" ? null : $("f-series").value,
      tags: splitLines($("f-tags").value),
      sources: {
        nga: splitLines($("f-nga").value),
        original_series: splitLines($("f-original-series").value),
        original: splitLines($("f-original").value),
        translation_series: splitLines($("f-translation-series").value),
        translation: splitLines($("f-translation").value),
      },
      body_text: $("f-body").value,
    };
  }

  function reviewSnapshot() {
    return JSON.stringify(collectReviewFields());
  }

  function updateDirty() {
    if (locked) {
      return;
    }
    const nowDirty = baseline !== null && reviewSnapshot() !== baseline;
    dirty = nowDirty;
    clear($("dirty-message"));
    $("discard-btn").hidden = !dirty;
    $("status-dirty").textContent = dirty ? "有未保存修改" : "";
    if (dirty) {
      const span = document.createElement("span");
      span.className = "error-text";
      span.textContent = "有未保存修改";
      $("dirty-message").appendChild(span);
      invalidateWriter();
    }
  }

  function guardDirty(action) {
    if (dirty) {
      pendingAction = action;
      showMessage($("detail-message"), "有未保存修改，请先保存或放弃后再继续。", true);
      return true;
    }
    return false;
  }

  function discardChanges() {
    const action = pendingAction;
    pendingAction = null;
    const id = state.selectedId;
    if (id === null) {
      return;
    }
    loadDetail(id).then(function (loaded) {
      if (loaded && action) {
        action();
      }
    });
  }

  async function saveReview() {
    if (state.selectedId === null || locked) {
      return;
    }
    try {
      const detail = await api(
        "/api/items/" + state.selectedId + "/review",
        jsonOptions("PUT", collectReviewFields())
      );
      renderMutationResult(detail);
      showMessage($("detail-message"), "已保存");
      refreshList();
    } catch (err) {
      showMessage($("detail-message"), err.message, true);
    }
  }

  async function importSubmission() {
    const rawText = $("import-raw").value;
    const canonicalUrl = $("import-url").value.trim() || null;
    try {
      const detail = await api(
        "/api/import",
        jsonOptions("POST", { raw_text: rawText, canonical_url: canonicalUrl })
      );
      state.selectedId = detail.id;
      renderMutationResult(detail);
      showMessage($("import-message"), "已导入 #" + detail.id + "，状态: " + detail.status);
      refreshList();
    } catch (err) {
      showMessage($("import-message"), err.message, true);
    }
  }

  function importGuarded() {
    if (guardDirty(importSubmission)) {
      return;
    }
    importSubmission();
  }

  async function changeStatus() {
    if (state.selectedId === null) {
      return;
    }
    const toStatus = $("target-status").value;
    const reason = $("transition-reason").value;
    try {
      const detail = await api(
        "/api/items/" + state.selectedId + "/transition",
        jsonOptions("POST", { to_status: toStatus, reason: reason })
      );
      renderMutationResult(detail);
      showMessage($("transition-message"), "状态已更新为 " + detail.status);
      refreshList();
    } catch (err) {
      showMessage($("transition-message"), err.message, true);
    }
  }

  function transitionGuarded() {
    if (guardDirty(changeStatus)) {
      return;
    }
    changeStatus();
  }

  /* ---------------- Writer UI ---------------- */

  function loadWriterConfig() {
    api("/api/writer/config")
      .then(function (config) {
        writerState.config = config;
        if (!config.enabled) {
          $("status-writer").textContent = "Writer 未配置";
          $("w-config").textContent = "启动时未配置 Writer（缺少 --umalib-db / --candidate-dir）";
        } else {
          $("status-writer").textContent =
            "Writer 已配置 · 源: " + config.source_file_name + " · 输出目录: " + config.candidate_dir_name;
          $("w-config").textContent =
            "源数据库（只读）: " + config.source_file_name +
            "　候选输出目录: " + config.candidate_dir_name;
        }
      })
      .catch(function (err) {
        $("status-writer").textContent = "Writer 未配置";
        $("w-config").textContent = "无法读取 Writer 配置: " + err.message;
      });
  }

  function resetWriterPanel(d, renderVersion) {
    const itemChanged = writerState.itemId !== d.id;
    writerState.previewRequestVersion += 1;
    writerState.itemId = d.id;
    clear($("w-plan-message"));
    clear($("w-plan-diagnostics"));
    clear($("w-preview-diagnostics"));
    clear($("w-operations"));
    clear($("w-hashes"));
    clear($("w-write-message"));
    $("w-close-confirmed").checked = false;
    writerState.plan = null;
    writerState.preview = null;
    if (itemChanged) {
      writerState.result = null;
      clear($("w-result"));
    }
    $("w-candidate-file").value = "umalib-candidate-item-" + String(d.id) + ".db";
    $("w-upload-time").value = "";
    $("w-upload-seconds").textContent = "";
    $("w-note").value = "";
    renderSourceOptions(d);
    renderTagDecisions(d);
    updateWriterPanelState(d);
  }

  function updateWriterPanelState(d) {
    const enabled = writerState.config !== null && writerState.config.enabled;
    $("writer-panel").hidden = d.status !== "approved";
    if (d.status !== "approved") {
      return;
    }
    const controls = [
      "w-candidate-file",
      "w-upload-time",
      "w-note",
      "w-source-available",
      "w-source-add-btn",
      "w-preview-btn",
    ];
    for (const id of controls) {
      $(id).disabled = !enabled || writerState.writeBusy;
    }
    $("w-catalog-btn").disabled = !enabled || writerState.writeBusy;
    for (const control of document.querySelectorAll(
      "#w-source-selected button, #w-tag-decisions select"
    )) {
      control.disabled = !enabled || writerState.writeBusy;
    }
    $("transition-btn").disabled = writerState.writeBusy;
    updateWriteButton();
  }

  function invalidateWriter() {
    if (writerState.plan === null && writerState.preview === null && writerState.result === null) {
      return;
    }
    writerState.plan = null;
    writerState.preview = null;
    writerState.result = null;
    $("w-close-confirmed").checked = false;
    clear($("w-plan-message"));
    clear($("w-plan-diagnostics"));
    clear($("w-preview-diagnostics"));
    clear($("w-operations"));
    clear($("w-hashes"));
    clear($("w-result"));
    clear($("w-write-message"));
    updateWriteButton();
  }

  function loadCatalog() {
    const requestVersion = ++writerState.catalogRequestVersion;
    writerState.catalogLoading = true;
    showMessage($("w-plan-message"), "正在重新加载标签目录…");
    api("/api/writer/catalog")
      .then(function (catalog) {
        if (requestVersion !== writerState.catalogRequestVersion) {
          return;
        }
        writerState.catalog = catalog;
        writerState.catalogLoading = false;
        $("w-catalog-summary").textContent =
          "数据库 SHA: " + catalog.database_sha256.slice(0, 12) + "… · " +
          "Schema 指纹: " + catalog.schema_fingerprint_sha256.slice(0, 12) + "… · " +
          "标签 " + catalog.tags.length + " 个";
        invalidateWriter();
        renderTagDecisions(state.lastDetail);
        showMessage($("w-plan-message"), "标签目录已更新");
      })
      .catch(function (err) {
        if (requestVersion !== writerState.catalogRequestVersion) {
          return;
        }
        writerState.catalogLoading = false;
        showMessage($("w-plan-message"), err.message, true);
      });
  }

  function renderSourceOptions(d) {
    const available = [];
    const sources = d.review_fields.sources;
    for (const key of ["nga", "original_series", "original", "translation_series", "translation"]) {
      for (const url of sources[key]) {
        if (available.indexOf(url) === -1) {
          available.push(url);
        }
      }
    }
    const select = $("w-source-available");
    clear(select);
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = available.length ? "选择要加入的来源 URL" : "（无可用来源）";
    placeholder.disabled = available.length === 0;
    select.appendChild(placeholder);
    for (const url of available) {
      const option = document.createElement("option");
      option.value = url;
      option.textContent = url;
      select.appendChild(option);
    }
    clear($("w-source-selected"));
  }

  function collectSelectedSources() {
    const list = $("w-source-selected");
    const urls = [];
    for (const child of list.children) {
      urls.push(child.firstChild.textContent);
    }
    return urls;
  }

  function moveSource(index, delta) {
    const urls = collectSelectedSources();
    const target = index + delta;
    if (target < 0 || target >= urls.length) {
      return;
    }
    const tmp = urls[index];
    urls[index] = urls[target];
    urls[target] = tmp;
    renderSourceRows(urls);
    invalidateWriter();
  }

  function removeSource(index) {
    const urls = collectSelectedSources();
    urls.splice(index, 1);
    renderSourceRows(urls);
    invalidateWriter();
  }

  function renderSourceRows(urls) {
    const list = $("w-source-selected");
    clear(list);
    for (let index = 0; index < urls.length; index += 1) {
      const item = document.createElement("li");
      const text = document.createElement("span");
      text.textContent = urls[index];
      const up = document.createElement("button");
      up.type = "button";
      up.className = "small";
      up.textContent = "↑";
      up.disabled = writerState.writeBusy;
      up.addEventListener("click", function () {
        moveSource(index, -1);
      });
      const down = document.createElement("button");
      down.type = "button";
      down.className = "small";
      down.textContent = "↓";
      down.disabled = writerState.writeBusy;
      down.addEventListener("click", function () {
        moveSource(index, 1);
      });
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "small";
      remove.textContent = "移除";
      remove.disabled = writerState.writeBusy;
      remove.addEventListener("click", function () {
        removeSource(index);
      });
      item.appendChild(text);
      item.appendChild(up);
      item.appendChild(down);
      item.appendChild(remove);
      list.appendChild(item);
    }
  }

  function addSelectedSource() {
    const select = $("w-source-available");
    const url = select.value;
    if (!url) {
      return;
    }
    const urls = collectSelectedSources();
    if (urls.indexOf(url) !== -1) {
      showMessage($("w-plan-message"), "该来源已在列表中", true);
      return;
    }
    urls.push(url);
    renderSourceRows(urls);
    invalidateWriter();
  }

  function collectTagRequests(d) {
    const requests = [];
    for (const value of d.review_fields.tags) {
      requests.push({ kind: "tag", value: value });
    }
    if (d.review_fields.series) {
      requests.push({ kind: "series", value: d.review_fields.series });
    }
    return requests;
  }

  function renderTagDecisions(d) {
    const container = $("w-tag-decisions");
    clear(container);
    if (!d) {
      return;
    }
    if (!writerState.catalog) {
      const p = document.createElement("p");
      p.className = "muted";
      p.textContent = "请先点击“重新加载标签目录”后再做标签决策（后端会重新校验，这里只做人工选择）。";
      container.appendChild(p);
      return;
    }
    const requests = collectTagRequests(d);
    if (requests.length === 0) {
      const p = document.createElement("p");
      p.className = "muted";
      p.textContent = "（无标签/系列需要决策）";
      container.appendChild(p);
      return;
    }
    for (const request of requests) {
      const row = document.createElement("div");
      row.className = "w-tag-row";
      const kind = document.createElement("span");
      kind.className = "w-tag-kind";
      kind.textContent = request.kind === "tag" ? "标签" : "系列";
      const value = document.createElement("span");
      value.className = "w-tag-value";
      value.textContent = request.value;
      const select = document.createElement("select");
      select.disabled = writerState.writeBusy;
      const empty = document.createElement("option");
      empty.value = "";
      empty.textContent = "（自动判断）";
      select.appendChild(empty);
      const matches = writerState.catalog.tags.filter(function (tag) {
        return tag.name === request.value;
      });
      for (const tag of matches) {
        const option = document.createElement("option");
        option.value = "existing:" + String(tag.id);
        option.textContent =
          "复用现有标签 id=" + tag.id + "（类型 " + tag.type + "，关联 " + tag.usage_count + "）";
        if (matches.length === 1) {
          option.selected = true;
        }
        select.appendChild(option);
      }
      if (matches.length === 0) {
        for (let tagType = 0; tagType <= 4; tagType += 1) {
          const option = document.createElement("option");
          option.value = "new:" + String(tagType);
          option.textContent = "新建标签（type " + tagType + "）";
          select.appendChild(option);
        }
      }
      const aliasGroup = document.createElement("optgroup");
      aliasGroup.label = "或显式选择其他现有标签（别名）";
      for (const tag of writerState.catalog.tags) {
        const option = document.createElement("option");
        option.value = "existing:" + String(tag.id);
        option.textContent = "id=" + tag.id + " · " + tag.name + "（类型 " + tag.type + "）";
        aliasGroup.appendChild(option);
      }
      select.appendChild(aliasGroup);
      select.addEventListener("change", invalidateWriter);
      row.appendChild(kind);
      row.appendChild(value);
      row.appendChild(select);
      container.appendChild(row);
    }
  }

  function collectTagChoices() {
    const container = $("w-tag-decisions");
    if (!state.lastDetail) {
      return [];
    }
    const requests = collectTagRequests(state.lastDetail);
    const rows = container.getElementsByClassName("w-tag-row");
    const choices = [];
    for (let index = 0; index < rows.length && index < requests.length; index += 1) {
      const select = rows[index].querySelector("select");
      if (!select || select.value === "") {
        continue;
      }
      const request = requests[index];
      const choice = {
        requested_kind: request.kind,
        requested_value: request.value,
        existing_tag_id: null,
        new_tag_type: null,
      };
      if (select.value.indexOf("existing:") === 0) {
        choice.existing_tag_id = parseInt(select.value.slice(9), 10);
      } else if (select.value.indexOf("new:") === 0) {
        choice.new_tag_type = parseInt(select.value.slice(4), 10);
      }
      choices.push(choice);
    }
    return choices;
  }

  function collectChoicesPayload() {
    const uploadSeconds = localDateTimeToUnixSeconds($("w-upload-time").value);
    return {
      candidate_file_name: $("w-candidate-file").value,
      choices: {
        upload_time_seconds: isNaN(uploadSeconds) ? 0 : uploadSeconds,
        note: $("w-note").value === "" ? null : $("w-note").value,
        ordered_sources: collectSelectedSources(),
        tag_choices: collectTagChoices(),
      },
    };
  }

  function updateWriteButton() {
    const previewReady =
      writerState.plan !== null &&
      writerState.preview !== null &&
      writerState.plan.ready === true &&
      writerState.preview.ready_to_write === true;
    $("w-close-confirmed").disabled = !previewReady || writerState.writeBusy;
    if (!previewReady) {
      $("w-close-confirmed").checked = false;
    }
    const ready =
      previewReady &&
      $("w-close-confirmed").checked === true &&
      !writerState.writeBusy;
    $("w-write-btn").disabled = !ready;
  }

  function renderDiagnostics(node, label, blockers, warnings) {
    clear(node);
    const heading = document.createElement("p");
    heading.className = "muted";
    heading.textContent = label;
    node.appendChild(heading);
    if (blockers.length === 0 && warnings.length === 0) {
      const none = document.createElement("ul");
      none.appendChild(li("（无）"));
      node.appendChild(none);
      return;
    }
    const list = document.createElement("ul");
    for (const b of blockers) {
      const span = document.createElement("span");
      span.className = "severity-blocker";
      span.textContent = "⛔ blocker [" + b.code + "] " + b.message + "（" + b.field + "）";
      const item = document.createElement("li");
      item.appendChild(span);
      list.appendChild(item);
    }
    for (const w of warnings) {
      const span = document.createElement("span");
      span.className = "severity-warning";
      span.textContent = "⚠ warning [" + w.code + "] " + w.message + "（" + w.field + "）";
      const item = document.createElement("li");
      item.appendChild(span);
      list.appendChild(item);
    }
    node.appendChild(list);
  }

  function renderOperations(operations) {
    const node = $("w-operations");
    clear(node);
    const createTags = operations.create_tags
      .map(function (t) {
        return t.name + "/type " + t.type;
      })
      .join("、");
    const ul = document.createElement("ul");
    ul.appendChild(li("article 新增: " + operations.article_insert_count + " 篇"));
    ul.appendChild(li("复用现有标签 ID: " + (operations.existing_tag_ids.length ? operations.existing_tag_ids.join(", ") : "（无）")));
    ul.appendChild(li("新建标签: " + (createTags || "（无）")));
    ul.appendChild(li("tagged 新增: " + operations.tagged_insert_count + " 条"));
    node.appendChild(ul);
  }

  async function previewWriter() {
    if (state.selectedId === null) {
      return;
    }
    if (guardDirty(previewWriter)) {
      return;
    }
    const itemId = state.selectedId;
    const uploadSeconds = localDateTimeToUnixSeconds($("w-upload-time").value);
    if (isNaN(uploadSeconds)) {
      showMessage($("w-plan-message"), "请先填写有效的上传时间", true);
      return;
    }
    const payload = collectChoicesPayload();
    if (payload.candidate_file_name === "") {
      showMessage($("w-plan-message"), "请填写候选文件名", true);
      return;
    }
    const requestVersion = ++writerState.previewRequestVersion;
    showMessage($("w-plan-message"), "正在生成 dry-run 预览…");
    try {
      const data = await api(
        "/api/items/" + itemId + "/writer/preview",
        jsonOptions("POST", payload)
      );
      if (
        requestVersion !== writerState.previewRequestVersion ||
        state.selectedId !== itemId
      ) {
        return;
      }
      writerState.plan = data.plan;
      writerState.preview = data.preview;
      writerState.result = null;
      $("w-close-confirmed").checked = false;
      clear($("w-result"));
      clear($("w-write-message"));
      const planBlockers = data.plan.diagnostics.blockers;
      const planWarnings = data.plan.diagnostics.warnings;
      const previewBlockers = data.preview.diagnostics.blockers;
      const previewWarnings = data.preview.diagnostics.warnings;
      $("w-plan-message").textContent = "";
      showMessage($("w-plan-message"), "dry-run 完成");
      renderDiagnostics($("w-plan-diagnostics"), "计划诊断", planBlockers, planWarnings);
      renderDiagnostics($("w-preview-diagnostics"), "Writer 诊断", previewBlockers, previewWarnings);
      renderOperations(data.preview.operations);
      $("w-hashes").textContent =
        "plan_sha256: " + data.plan.plan_sha256 +
        "　preview_sha256: " + data.preview.preview_sha256 +
        "　catalog_sha256: " + data.plan.input.target_tag_catalog_sha256 +
        "　source_sha256: " + data.preview.source.database_sha256;
      updateWriteButton();
    } catch (err) {
      if (
        requestVersion !== writerState.previewRequestVersion ||
        state.selectedId !== itemId
      ) {
        return;
      }
      writerState.plan = null;
      writerState.preview = null;
      clear($("w-plan-diagnostics"));
      clear($("w-preview-diagnostics"));
      clear($("w-operations"));
      clear($("w-hashes"));
      showMessage($("w-plan-message"), err.message, true);
      updateWriteButton();
    }
  }

  async function writeCandidate() {
    if (state.selectedId === null) {
      return;
    }
    if (writerState.plan === null || writerState.preview === null) {
      return;
    }
    const itemId = state.selectedId;
    const requestVersion = ++writerState.writeRequestVersion;
    const payload = collectChoicesPayload();
    payload.expected_plan_sha256 = writerState.plan.plan_sha256;
    payload.expected_preview_sha256 = writerState.preview.preview_sha256;
    payload.expected_source_database_sha256 = writerState.preview.source.database_sha256;
    payload.app_closed_confirmed = $("w-close-confirmed").checked === true;
    writerState.writeBusy = true;
    updateWriterPanelState(state.lastDetail);
    showMessage($("w-write-message"), "正在生成候选数据库…");
    try {
      const data = await api(
        "/api/items/" + itemId + "/writer/write",
        jsonOptions("POST", payload)
      );
      if (
        requestVersion !== writerState.writeRequestVersion ||
        state.selectedId !== itemId
      ) {
        writerState.writeBusy = false;
        updateWriterPanelState(state.lastDetail);
        refreshList();
        return;
      }
      writerState.writeBusy = false;
      writerState.result = data.result;
      renderWriteResult(data);
      renderMutationResult(data.item);
      refreshList();
    } catch (err) {
      if (
        requestVersion !== writerState.writeRequestVersion ||
        state.selectedId !== itemId
      ) {
        writerState.writeBusy = false;
        updateWriterPanelState(state.lastDetail);
        refreshList();
        return;
      }
      writerState.writeBusy = false;
      updateWriterPanelState(state.lastDetail);
      updateWriteButton();
      showMessage($("w-write-message"), err.message, true);
    }
  }

  function renderWriteResult(data) {
    const result = data.result;
    const node = $("w-result");
    clear(node);
    const box = document.createElement("div");
    box.className = "w-result-box";
    const notice = document.createElement("p");
    notice.className = "w-notice";
    notice.textContent = "候选已生成；正式库未被替换，请用大书库人工打开验收。";
    box.appendChild(notice);
    const ul = document.createElement("ul");
    ul.appendChild(li("候选文件: " + result.candidate.file_name));
    ul.appendChild(li("候选 SHA-256: " + result.candidate.database_sha256));
    ul.appendChild(li("article id: " + result.inserted.article_id));
    ul.appendChild(li(
      "新建标签: " +
      result.inserted.created_tags
        .map(function (t) { return "id=" + t.id + "·" + t.name + "/type " + t.type; })
        .join("、") || "（无）"
    ));
    ul.appendChild(li("复用标签 ID: " + (result.inserted.reused_tag_ids.length ? result.inserted.reused_tag_ids.join(", ") : "（无）")));
    ul.appendChild(li(
      "计数: article " + result.counts_before.article + "→" + result.counts_after.article +
      "，tag " + result.counts_before.tag + "→" + result.counts_after.tag +
      "，tagged " + result.counts_before.tagged + "→" + result.counts_after.tagged
    ));
    ul.appendChild(li(
      "检查: integrity=" + (result.checks.integrity_check ? "ok" : "失败") +
      "，外键违规 " + result.checks.foreign_key_violation_count +
      "，重复关联 " + result.checks.duplicate_tagged_pair_count +
      "，inspector blocker " + result.checks.inspector_blocker_count
    ));
    ul.appendChild(li("result_sha256: " + result.result_sha256));
    ul.appendChild(li("源库未变: " + (result.source.unchanged ? "是" : "否")));
    box.appendChild(ul);
    node.appendChild(box);
  }

  /* ---------------- Watcher UI ---------------- */

  const watcherState = { busy: false, requestPending: false, loginConfirming: false, lastError: null };

  function loadWatcherConfig() {
    api("/api/watcher/config")
      .then(function (config) {
        if (!config.enabled) {
          $("wch-config").textContent = "启动时未配置 Watcher（缺少 --watcher-state / --watcher-profile / --watcher-snapshot-dir）";
          setWatcherButtonsDisabled(true);
        } else {
          $("wch-config").textContent = "固定帖子 tid=" + config.tid + "；扫描只把投稿放进 Inbox，不批准、不写大书库。";
          setWatcherButtonsDisabled(false);
          refreshWatcherStatus();
        }
      })
      .catch(function (err) {
        $("wch-config").textContent = "无法读取 Watcher 配置: " + err.message;
        setWatcherButtonsDisabled(true);
      });
  }

  function setWatcherButtonsDisabled(disabled) {
    for (const id of ["wch-refresh-btn", "wch-dryrun-btn", "wch-scan-btn", "wch-login-confirm-btn"]) {
      $(id).disabled = disabled;
    }
  }

  function refreshWatcherStatus() {
    return api("/api/watcher/status")
      .then(function (status) {
        if (watcherShaCache !== status.state_sha256) {
          $("wch-confirm-import").checked = false;
        }
        watcherShaCache = status.state_sha256;
        $("wch-status").textContent =
          "上次页码: " + status.last_page +
          " · 已知楼层: " + status.known_floor_count +
          " · 已导入: " + status.imported_count +
          " · 待人工复查: " + status.changed_after_import_count +
          " · state SHA: " + status.state_sha256.slice(0, 12) + "…" +
          " · 上次扫描: " + (status.last_successful_scan_at || "无");
        if (status.busy || status.awaiting_login) {
          watcherState.busy = true;
        } else {
          watcherState.busy = false;
        }
        updateWatcherScanState(status);
        if (status.last_result) {
          renderWatcherResult(status.last_result);
        }
        if (status.last_error) {
          renderWatcherError(status.last_error);
        } else if (watcherState.lastError) {
          renderWatcherError(watcherState.lastError);
        } else if (status.last_result && !status.busy) {
          showMessage($("wch-scan-state"), "扫描完成");
        }
      })
      .catch(function (err) {
        watcherShaCache = null;
        $("wch-confirm-import").checked = false;
        $("wch-dryrun-btn").disabled = true;
        $("wch-scan-btn").disabled = true;
        $("wch-login-confirm-btn").disabled = true;
        $("wch-status").textContent = "状态不可用: " + err.message;
      });
  }

  function updateWatcherScanState(status) {
    const node = $("wch-scan-state");
    if (status.busy || status.awaiting_login) clear(node);
    $("wch-login-wait").hidden = true;
    $("wch-refresh-btn").disabled = false;
    for (const id of ["wch-dryrun-btn", "wch-scan-btn"]) {
      $(id).disabled = status.busy || watcherState.requestPending || !status.enabled;
    }
    $("wch-login-confirm-btn").disabled = !status.awaiting_login || watcherState.loginConfirming;
    if (status.awaiting_login) {
      $("wch-login-wait").hidden = false;
    }
    if (status.busy && !status.awaiting_login) {
      const span = document.createElement("span");
      span.textContent = "扫描中…";
      node.appendChild(span);
    }
  }

  function renderWatcherResult(result) {
    const node = $("wch-result");
    clear(node);
    const box = document.createElement("div");
    box.className = "w-result-box";
    const ul = document.createElement("ul");
    ul.appendChild(li(
      "页范围: " + (result.previous_last_page === null ? "-" : result.previous_last_page) +
      " → " + (result.current_last_page === null ? "-" : result.current_last_page) +
      "（扫描 " + result.scanned_pages.join(",") + "）"
    ));
    ul.appendChild(li(
      "楼层: 新增 " + result.new_floor_count +
      " · 未变 " + result.unchanged_floor_count +
      " · 变化 " + result.changed_floor_count
    ));
    ul.appendChild(li(
      "普通讨论: " + result.discussion_count +
      " · 触发 [收录]: " + result.triggered_count
    ));
    ul.appendChild(li(
      "导入: " +
      (result.imported_items.length
        ? result.imported_items.map(function (item) {
            return "#" + item.item_id + "(" + item.status + ")";
          }).join(", ")
        : "无")
    ));
    ul.appendChild(li(
      "待人工复查 item: " +
      (result.changed_after_import.length ? result.changed_after_import.join(", ") : "无")
    ));
    ul.appendChild(li(
      "diagnostics: " + (result.diagnostics.length ? result.diagnostics.join(", ") : "无")
    ));
    box.appendChild(ul);
    node.appendChild(box);
  }

  function renderWatcherError(error) {
    const node = $("wch-scan-state");
    clear(node);
    const span = document.createElement("span");
    if (error.code === "watcher_shutdown") {
      span.className = "severity-warning";
      span.textContent = "已取消：" + error.message;
    } else if (error.code === "watcher_partial_import") {
      span.className = "severity-warning";
      span.textContent = "已部分入队，需恢复：" + error.message;
    } else {
      span.className = "error-text";
      span.textContent = "[" + error.code + "] " + error.message;
    }
    node.appendChild(span);
  }

  async function watcherScan(dryRun) {
    if (watcherState.requestPending || watcherState.busy) return;
    if (dirty) {
      showMessage($("wch-scan-state"), "有未保存审核修改，请先保存或放弃后再扫描。", true);
      return;
    }
    if (!dryRun) {
      if ($("wch-confirm-import").checked !== true) {
        showMessage($("wch-scan-state"), "请先勾选“我确认把本次发现的投稿加入当前 Inbox，并推进扫描状态”。", true);
        return;
      }
    }
    const payload = { dry_run: dryRun, confirm_import: false, expected_state_sha256: null };
    if (!dryRun) {
      payload.confirm_import = true;
      payload.expected_state_sha256 = watcherShaCache;
      if (!payload.expected_state_sha256) {
        showMessage($("wch-scan-state"), "请先刷新状态取得当前 state SHA。", true);
        return;
      }
    }
    watcherState.busy = true;
    watcherState.requestPending = true;
    watcherState.loginConfirming = false;
    watcherState.lastError = null;
    $("wch-confirm-import").checked = false;
    for (const id of ["wch-dryrun-btn", "wch-scan-btn", "wch-login-confirm-btn"]) {
      $(id).disabled = true;
    }
    $("wch-login-wait").hidden = true;
    showMessage($("wch-scan-state"), dryRun ? "dry-run 扫描中…" : "扫描并导入中…");
    const poll = setInterval(function () {
      api("/api/watcher/status")
        .then(function (status) {
          if (watcherState.requestPending) updateWatcherScanState(status);
        })
        .catch(function () {});
    }, 2000);
    try {
      const result = await api("/api/watcher/scan", jsonOptions("POST", payload));
      clearInterval(poll);
      watcherState.busy = false;
      watcherState.requestPending = false;
      renderWatcherResult(result);
      showMessage($("wch-scan-state"), "扫描完成");
      refreshWatcherStatus();
      refreshList();
    } catch (err) {
      clearInterval(poll);
      watcherState.busy = false;
      watcherState.requestPending = false;
      watcherState.lastError = { code: err.code || "unknown", message: err.message };
      renderWatcherError(watcherState.lastError);
      if (err.code === "watcher_partial_import") refreshList();
      refreshWatcherStatus();
    }
  }

  async function watcherLoginConfirm() {
    if (watcherState.loginConfirming) return;
    watcherState.loginConfirming = true;
    $("wch-login-confirm-btn").disabled = true;
    try {
      await api("/api/watcher/login-confirm", jsonOptions("POST", {}));
      showMessage($("wch-scan-state"), "已确认登录，正在重试一次…");
    } catch (err) {
      watcherState.loginConfirming = false;
      showMessage($("wch-scan-state"), err.message, true);
    }
  }

  /* ---------------- events ---------------- */

  function bindEvents() {
    $("import-btn").addEventListener("click", importGuarded);
    $("save-btn").addEventListener("click", saveReview);
    $("discard-btn").addEventListener("click", discardChanges);
    $("transition-btn").addEventListener("click", transitionGuarded);
    $("status-filter").addEventListener("change", function () {
      refreshList();
    });
    $("search-input").addEventListener("input", function () {
      renderItems();
    });
    for (const id of reviewControlIds) {
      $(id).addEventListener("input", updateDirty);
      $(id).addEventListener("change", updateDirty);
    }
    $("w-catalog-btn").addEventListener("click", loadCatalog);
    $("w-source-add-btn").addEventListener("click", addSelectedSource);
    $("w-preview-btn").addEventListener("click", previewWriter);
    $("w-write-btn").addEventListener("click", writeCandidate);
    $("w-close-confirmed").addEventListener("change", updateWriteButton);
    for (const id of ["w-candidate-file", "w-upload-time", "w-note"]) {
      $(id).addEventListener("input", function () {
        invalidateWriter();
        const seconds = localDateTimeToUnixSeconds($("w-upload-time").value);
        $("w-upload-seconds").textContent =
          isNaN(seconds) && $("w-upload-time").value !== ""
            ? "时间格式无效"
            : "Unix 秒: " + seconds;
      });
    }
    $("wch-refresh-btn").addEventListener("click", refreshWatcherStatus);
    $("wch-dryrun-btn").addEventListener("click", function () {
      watcherScan(true);
    });
    $("wch-scan-btn").addEventListener("click", function () {
      watcherScan(false);
    });
    $("wch-login-confirm-btn").addEventListener("click", watcherLoginConfirm);
  }

  function init() {
    bindEvents();
    loadWriterConfig();
    loadWatcherConfig();
    refreshList();
  }

  init();
})();
