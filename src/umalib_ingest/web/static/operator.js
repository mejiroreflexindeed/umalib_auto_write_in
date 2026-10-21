/* TASK-014 产品页面脚本：状态、事件、HTTP、dirty guard（不含业务规则）。 */
"use strict";

(function () {
var state = {
  workspaceId: null,
  threads: { revision: 0, active_tid: null, entries: [] },
  activeTid: null,
  statusTid: null,
  currentRunId: null,
  selectedItemId: null,
  syncSaveEnabled: function () {},
    dirty: false,
    choicesDirty: false,
    editVersion: 0,
    previewRequestVersion: 0,
    previewId: null,
    operationId: null,
    writerHashes: null,
    previewReady: false,
    pendingSourceId: null,
    detailRequestVersion: 0,
  };

  function $(id) { return document.getElementById(id); }

  function api(path, options) {
    return fetch(path, options || {}).then(function (res) {
      return res.json().then(function (body) {
        if (body && body.error) {
          var err = new Error("[" + body.error.code + "] " + body.error.message);
          err.code = body.error.code;
          err.retryable = body.error.retryable;
          throw err;
        }
        return body.data;
      });
    });
  }

  function json(method, body) {
    return {
      method: method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    };
  }

  function epochToLocalInput(seconds) {
    var date = seconds ? new Date(seconds * 1000) : new Date();
    var shifted = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
    return shifted.toISOString().slice(0, 16);
  }

  function localInputToEpoch(value) {
    var millis = Date.parse(value);
    return Number.isFinite(millis) ? Math.floor(millis / 1000) : null;
  }

  function splitLines(value) {
    return (value || "").split(/\r?\n/).map(function (s) { return s.trim(); }).filter(Boolean);
  }

  var STATUS_LABELS = {
    candidate: "待审",
    incomplete: "信息不全",
    suspected_duplicate: "疑似重复",
    approved: "已批准",
    exported: "已写入候选",
    ignored: "已忽略",
  };

  function statusLabel(status) {
    return STATUS_LABELS[status] || status || "";
  }

  /* UTC ISO → 固定 UTC+08:00 日历日期（不依赖浏览器时区、不截断 UTC 字符串）。 */
  function utcIsoToLocalDate(iso) {
    var millis = Date.parse(iso);
    if (!Number.isFinite(millis)) { return ""; }
    return new Date(millis + 8 * 3600 * 1000).toISOString().slice(0, 10);
  }

  /* G04：把已安全子集（p/br/strong/em/u/s/span[color]）渲染为 DOM；不使用 innerHTML。 */
  function renderQuillSafely(target, html) {
    var doc = new DOMParser().parseFromString(String(html || ""), "text/html");
    var nodes = doc.body.childNodes;
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      if (node.nodeType === 1 && node.tagName === "P") {
        var paragraph = document.createElement("p");
        appendSafeRuns(paragraph, node);
        target.appendChild(paragraph);
      }
    }
  }

  function appendSafeRuns(target, htmlNode) {
    for (var i = 0; i < htmlNode.childNodes.length; i++) {
      var child = htmlNode.childNodes[i];
      if (child.nodeType === 3) {
        target.appendChild(document.createTextNode(child.textContent));
        continue;
      }
      if (child.nodeType !== 1) { continue; }
      var tag = child.tagName;
      if (tag === "BR") { target.appendChild(document.createElement("br")); continue; }
      var inner = document.createElement("span");
      if (tag === "STRONG") { inner.style.fontWeight = "bold"; }
      else if (tag === "EM") { inner.style.fontStyle = "italic"; }
      else if (tag === "U") { inner.style.textDecoration = "underline"; }
      else if (tag === "S") { inner.style.textDecoration = "line-through"; }
      else if (tag === "SPAN") {
        var match = /^color:\s*(#[0-9a-f]{6});?$/i.exec(
          (child.getAttribute("style") || "").trim());
        if (match) { inner.style.color = match[1]; }
        else {
          inner.textContent = child.textContent;
          target.appendChild(inner);
          continue;
        }
      } else {
        target.appendChild(document.createTextNode(child.textContent));
        continue;
      }
      appendSafeRuns(inner, child);
      target.appendChild(inner);
    }
  }

  function hasUnsaved() { return state.dirty || state.choicesDirty; }

  function invalidatePreview() {
    state.previewId = null;
    state.previewReady = false;
    state.previewRequestVersion += 1;
    var write = document.querySelector(".writer-write");
    if (write) { write.disabled = true; }
    var confirm = document.querySelector(".writer-confirm");
    if (confirm) { confirm.checked = false; }
    var preview = document.querySelector(".writer-preview-inline");
    if (preview) {
      preview.textContent = "内容或人工决定已变化；请保存后重新生成预览";
      preview.classList.add("hidden");
    }
  }

  /* 未保存保护：所有离开当前编辑稿的路径都必须经过这里。 */
  function dirtyGuard(proceed) {
    if (!hasUnsaved()) { proceed(); return; }
    if (window.confirm("当前稿件有未保存的修改，确定离开并放弃这些修改吗？")) {
      state.dirty = false;
      state.choicesDirty = false;
      state.selectedItemId = null;
      state.detailRequestVersion += 1;
      state.editVersion += 1;
      invalidatePreview();
      $("review-panel").classList.add("hidden");
      $("review-form").textContent = "";
      proceed();
    }
  }

  function msg(node, text, kind) {
    node.textContent = "";
    var span = document.createElement("span");
    span.className = kind || "";
    span.textContent = text;
    node.appendChild(span);
  }

  function setEnabled(id, on, reason) {
    var btn = $(id);
    btn.disabled = !on;
    btn.title = "";
    if (!on && reason) {
      var note = btn.parentNode.querySelector(".disabled-reason-" + id) || (function () {
        var s = document.createElement("span");
        s.className = "muted";
        btn.parentNode.insertBefore(s, btn.nextSibling);
        return s;
      })();
      note.className = "muted disabled-reason-" + id;
      note.textContent = " " + reason;
    }
  }

  function showSection(name) {
    if (name === "threads" || name === "delivery" || name === "settings") {
      // 离开审核稿：保护未保存修改
      if (state.selectedItemId && hasUnsaved()) {
        dirtyGuard(function () { showSectionNow(name); });
        return;
      }
    }
    showSectionNow(name);
  }

  function showSectionNow(name) {
    ["threads", "inbox", "delivery", "settings"].forEach(function (s) {
      $(s + "-page").classList.toggle("hidden", s !== name);
    });
    document.querySelectorAll("#main-nav button").forEach(function (b) {
      b.classList.toggle("active", b.dataset.section === name);
    });
    if (name === "inbox") { loadInbox(); }
    if (name === "delivery") { loadDeliveries(); }
    if (name === "settings") { loadSettings(); }
  }

  function loadSettings() {
    api("/api/operator/status").then(function (st) {
      var summary = "工作区：" + st.workspace_name + "（" + st.workspace_id.slice(0, 8)
        + "…，revision " + st.revision + "）｜副本："
        + (st.source.bound ? st.source.file_name + "（sha256 "
            + String(st.source.sha256 || "").slice(0, 12) + "…）" : "未绑定");
      $("settings-summary").textContent = summary;
    }).catch(function () {});
    api("/api/operator/workspaces").then(function (data) {
      var box = $("workspace-list");
      box.textContent = "";
      data.workspaces.forEach(function (entry) {
        var row = document.createElement("div");
        row.textContent = (entry.current ? "★ " : "") + entry.name + "（"
          + entry.workspace_id.slice(0, 8) + "…，" + entry.mode + "）";
        if (!entry.current) {
          var open = document.createElement("button");
          open.textContent = "打开此工作区（重启后生效）";
          open.addEventListener("click", function () {
            if (!window.confirm("切换工作区需要退出并重新启动助手。继续吗？")) { return; }
            api("/api/operator/workspaces/select", json("POST", {
              workspace_id: entry.workspace_id, confirmed: true,
            })).then(function () {
              msg($("workspace-msg"), "已选择：" + entry.name + "；请退出并重新启动助手进入", "ok");
            }).catch(function (e) { msg($("workspace-msg"), e.message, "error"); });
          });
          row.appendChild(open);
        }
        box.appendChild(row);
      });
      if (!data.workspaces.length) { box.textContent = "暂无工作区"; }
    }).catch(function (e) { msg($("workspace-msg"), e.message, "error"); });
    msg($("settings-help"),
        "· watcher_state_stale：状态被重新扫描推进，刷新状态后重新勾选确认即可。\n"
        + "· watcher_scan_busy / profile_busy：已有扫描或其他工作区占用浏览器；等待结束或关闭其他助手。\n"
        + "· watcher_locked：有残留锁；确认无扫描后由维护/重启恢复，不要手工改状态文件。\n"
        + "· 已取消：服务关闭取消扫描，不等于登录成功；重新打开再来。\n"
        + "· item_locked：已写入候选的稿件不可直接修改，可退回待审（未导出时）或基于新候选处理。\n"
        + "· maintenance_active：正在创建检查点，稍后重试。\n"
        + "· 旧楼编辑覆盖：扫描按楼层内容变化判断，历史日期之后的旧楼编辑不保证被发现；"
        + "需要覆盖时用“按较早日期补查”或“按页范围补查”。现阶段只生成候选副本，"
        + "不替换正式大书库。\n"
        + "· 运行记录与诊断：workspaces\\<工作区>\\threads\\<tid>\\runs\\ 下的 JSON（含状态与错误码）；"
        + "本机日志在同目录，克隆后证据根见交接文档。", "");
  }

  /* ---- 启动 ---- */

  function boot() {
    document.querySelectorAll("#main-nav button").forEach(function (b) {
      b.addEventListener("click", function () { showSection(b.dataset.section); });
    });
    api("/api/operator/status").then(function (st) {
      state.workspaceId = st.workspace_id;
      $("ws-summary").textContent = st.workspace_name + "（副本 " + (st.source.file_name || "未绑定") + "）";
      if (!st.source.bound) {
        showSourceWizard(st);
      } else {
        loadThreads();
        // F010：刷新时重新接入仍在运行的任务（取消按钮/提示/轮询）
        if (st.active_run) {
          resumeRun(st.active_run);
        }
      }
    }).catch(function (err) {
      msg($("status-diagnostic"), err.message, "error");
    });
  }

  function showSourceWizard(st) {
    $("source-wizard").classList.remove("hidden");
    if (st.source.file_name) {
      $("source-detect").textContent = "检测到默认副本：" + st.source.file_name;
      state.pendingSourceId = st.source.detected && st.source.detected.source_id;
    } else {
      $("source-detect").textContent = "未检测到默认 main-copy.db，请选择或输入副本路径。";
    }
    $("confirm-source-btn").addEventListener("click", function () {
      if (!state.pendingSourceId) {
        msg($("source-wizard-msg"), "请先选择并校验数据库副本", "warn");
        return;
      }
      api("/api/operator/workspaces", json("POST", { source_id: state.pendingSourceId, name: "录入工作区", confirmed: true }))
        .then(function () { location.reload(); })
        .catch(function (e) { msg($("source-wizard-msg"), e.message, "error"); });
    });
    $("pick-source-btn").addEventListener("click", function () {
      api("/api/operator/choose-source", json("POST", {})).then(function (r) {
        if (r.cancelled) { return; }
        state.pendingSourceId = r.source_id;
        msg($("source-wizard-msg"), "校验通过：" + r.file_name + "；请点击确认", "ok");
      }).catch(function (e) {
        $("source-path-input").classList.remove("hidden");
        msg($("source-wizard-msg"), e.message, "warn");
      });
    });
    $("validate-source-path-btn").addEventListener("click", function () {
      api("/api/operator/validate-source-path", json("POST", { path: $("source-path").value }))
        .then(function (r) {
          state.pendingSourceId = r.source_id;
          msg($("source-wizard-msg"), "校验通过：" + r.file_name + "（sha256 " + r.sha256.slice(0, 12) + "…）", "ok");
        }).catch(function (e) { msg($("source-wizard-msg"), e.message, "error"); });
    });
  }

  /* ---- 帖子 ---- */

  function loadThreads() {
    api("/api/operator/threads").then(function (data) {
      state.threads = data;
      state.activeTid = data.active_tid || (data.entries.length ? data.entries[0].tid : null);
      renderThreads();
      renderTidFilter();
      if (state.activeTid) { loadThreadStatus(state.activeTid); }
      else { $("add-thread-panel").classList.remove("hidden"); }
    });
  }

  function renderThreads() {
    var box = $("thread-entries");
    box.textContent = "";
    state.threads.entries.forEach(function (entry) {
      var row = document.createElement("div");
      row.className = "thread-entry" + (entry.tid === state.activeTid ? " active" : "")
        + (entry.enabled ? "" : " disabled");
      row.textContent = entry.name + "（tid " + entry.tid + (entry.enabled ? "" : "，已停用") + "）";
      var renameBtn = document.createElement("button");
      renameBtn.textContent = "改名";
      renameBtn.addEventListener("click", function (ev) {
        ev.stopPropagation();
        var value = window.prompt("输入新的帖子名称：", entry.name);
        if (value === null) { return; }
        if (!value.trim()) { msg($("thread-msg"), "名称不能为空", "error"); return; }
        api("/api/operator/threads/" + entry.tid, json("PATCH", {
          expected_index_revision: state.threads.revision, name: value.trim(),
        })).then(function () { loadThreads(); })
          .catch(function (e) { msg($("thread-msg"), e.message, "error"); });
      });
      var toggleBtn = document.createElement("button");
      toggleBtn.textContent = entry.enabled ? "停用" : "启用";
      toggleBtn.addEventListener("click", function (ev) {
        ev.stopPropagation();
        api("/api/operator/threads/" + entry.tid, json("PATCH", {
          expected_index_revision: state.threads.revision, enabled: !entry.enabled,
        })).then(function () { loadThreads(); })
          .catch(function (e) { msg($("thread-msg"), e.message, "error"); });
      });
      row.appendChild(renameBtn);
      row.appendChild(toggleBtn);
      row.addEventListener("click", function () {
        dirtyGuard(function () {
          api("/api/operator/threads/" + entry.tid, json("PATCH", {
            expected_index_revision: state.threads.revision,
            make_active: true,
          })).then(function (updated) {
            state.threads = { revision: updated.revision, active_tid: updated.active_tid,
              entries: updated.entries };
            state.activeTid = entry.tid;
            renderThreads();
            loadThreadStatus(entry.tid);
          }).catch(function (e) { msg($("thread-msg"), e.message, "error"); });
        });
      });
      box.appendChild(row);
    });
    if (!state.threads.entries.length) {
      $("add-thread-panel").classList.remove("hidden");
    }
  }

  function renderTidFilter() {
    var sel = $("filter-tid");
    sel.textContent = "";
    var all = document.createElement("option");
    all.value = ""; all.textContent = "全部帖子";
    sel.appendChild(all);
    state.threads.entries.forEach(function (e) {
      var o = document.createElement("option");
      o.value = e.tid; o.textContent = e.name;
      sel.appendChild(o);
    });
  }

  $("add-thread-btn").addEventListener("click", function () {
    api("/api/operator/threads", json("POST", {
      url: $("thread-url").value, name: $("thread-name").value || null,
      expected_index_revision: state.threads.revision,
    })).then(function (r) {
      msg($("thread-msg"), "已添加帖子", "ok");
      loadThreads();
    }).catch(function (e) { msg($("thread-msg"), e.message, "error"); });
  });

  function loadThreadStatus(tid) {
    // 切换帖子时清空上一帖的提示与进程内预览，避免用旧预览确认新帖；
    // 同一帖刷新恢复时保留轮询与取消入口对应提示。
    state.initPreview = null;
    // 首载（statusTid 为空）保留 F010 刷新重接提示；真正换帖时才清旧提示。
    var switchedThread = state.statusTid !== null && state.statusTid !== tid;
    state.statusTid = tid;
    if (switchedThread || !state.currentRunId) {
      ["start-msg", "scan-msg", "auth-msg"].forEach(function (id) {
        if ($(id)) { $(id).textContent = ""; }
      });
      if ($("run-result-panel")) { $("run-result-panel").classList.add("hidden"); }
      if ($("rescan-msg")) { $("rescan-msg").textContent = ""; }
    }
    api("/api/operator/threads/" + tid + "/status").then(function (st) {
      $("thread-msg").textContent = st.name;
      state.configRevision = st.config_revision;
      ["start-panel", "auth-panel", "scan-panel"].forEach(function (p) {
        $(p).classList.remove("hidden");
      });
      if (st.start_confirmed && st.initialized) {
        $("start-panel").classList.add("hidden");
        $("initialize-btn").classList.add("hidden");
        $("rescan-panel").classList.remove("hidden");
        setEnabled("scan-btn", true);
        if (st.state) {
          var active = st.state.active_rescan;
          state.activeRescan = active || null;
          if (active) {
            // 刷新/重启后按持久 active_rescan 恢复原范围与继续入口
            var kindRadio = document.querySelector(
              'input[name="rescan-kind"][value="' + active.kind + '"]');
            if (kindRadio) { kindRadio.checked = true; }
            $("rescan-pages-input").classList.toggle("hidden", active.kind !== "pages");
            $("rescan-date-input").classList.toggle("hidden", active.kind !== "date");
            if (active.kind === "pages") {
              $("rescan-from").value = active.from_page;
              $("rescan-to").value = active.to_page;
            }
            if (active.kind === "date" && active.lower_bound_utc) {
              // 固定 UTC+08:00 回填，不用浏览器时区、不截断 UTC 字符串
              $("rescan-date").value = utcIsoToLocalDate(active.lower_bound_utc);
            }
            $("rescan-continue-btn").classList.remove("hidden");
            msg($("rescan-msg"), "检测到未完成的补查（" + active.kind + "，下一批从第 "
              + (active.next_page || active.from_page) + " 页起），可点“继续补查”按原范围续做",
              "warn");
          } else {
            if (!$("rescan-from").value && st.state.frontier_page >= 1) {
              $("rescan-from").value = 1;
              $("rescan-to").value = st.state.frontier_page;
            }
          }
          updateRescanCost();
        }
      } else {
        $("initialize-btn").classList.remove("hidden");
        setEnabled("scan-btn", false, "请先初始化扫描进度");
        if (st.start_confirmed) {
          msg($("start-msg"), "起点已确认，请点击初始化", "ok");
        }
      }
    }).catch(function (e) { msg($("thread-msg"), e.message, "error"); });
  }

  function currentStart() {
    var mode = document.querySelector('input[name="start-mode"]:checked').value;
    var page = mode === "page" ? parseInt($("start-page").value, 10) : null;
    var date = mode === "date" ? $("start-date").value || null : null;
    return { mode: mode, page: page, date: date, timezone: "UTC+08:00", lower_bound_utc: null };
  }

  document.querySelectorAll('input[name="start-mode"]').forEach(function (r) {
    r.addEventListener("change", function () {
      $("start-page-input").classList.toggle("hidden", r.value !== "page");
      $("start-date-input").classList.toggle("hidden", r.value !== "date");
    });
  });

  $("confirm-start-btn").addEventListener("click", function () {
    var tid = state.activeTid;
    api("/api/operator/threads/" + tid + "/initialize-preview", json("POST", {
      expected_config_revision: state.configRevision || 0,
      start: currentStart(),
      confirmed_network: true,
    })).then(function (r) {
      state.currentRunId = r.run_id;
      msg($("start-msg"), "正在读取…", "warn");
      pollRun(r.run_id, function (run) {
        if (run.status === "succeeded") {
          state.initPreview = run.result;
          msg($("start-msg"), "预览完成：" + (run.result.skip_note || "") + "；请点击“初始化扫描进度”", "ok");
        } else if (run.status === "failed" || run.status === "cancelled") {
          msg($("start-msg"), (run.error && run.error.message) || run.status, "error");
        }
      });
    }).catch(function (e) { msg($("start-msg"), e.message, "error"); });
  });

  $("login-btn").addEventListener("click", function () {
    api("/api/operator/threads/" + state.activeTid + "/login", json("POST", {
      confirmed_network: true,
    })).then(function (r) {
      state.currentRunId = r.run_id;
      msg($("auth-msg"), "正在打开并检查 NGA 登录状态…", "warn");
      pollRun(r.run_id, function (run) {
        $("login-confirm-btn").classList.toggle("hidden", run.status !== "awaiting_login");
        if (run.status === "awaiting_login") {
          msg($("auth-msg"), "请在已打开的 NGA 页面完成登录，再点击“我已完成登录”", "warn");
        } else if (run.status === "succeeded") {
          msg($("auth-msg"), "NGA 登录状态验证成功", "ok");
        } else if (["failed", "cancelled", "interrupted"].indexOf(run.status) >= 0) {
          msg($("auth-msg"), (run.error && run.error.message) || run.status, "error");
        }
      });
    }).catch(function (e) { msg($("auth-msg"), e.message, "error"); });
  });

  $("login-confirm-btn").addEventListener("click", function () {
    api("/api/operator/runs/" + state.currentRunId + "/login-confirm", json("POST", {
      confirmed: true,
    })).then(function () { msg($("auth-msg"), "已确认，正在验证", "warn"); })
      .catch(function (e) { msg($("auth-msg"), e.message, "error"); });
  });

  $("initialize-btn").addEventListener("click", function () {
    if (!state.initPreview) {
      msg($("scan-msg"), "请先在起点面板确认起点", "warn");
      return;
    }
    var tid = state.activeTid;
    api("/api/operator/threads/" + tid + "/initialize-confirm", json("POST", {
      run_id: state.currentRunId,
      preview_sha256: state.initPreview.preview_sha256,
      expected_config_revision: state.configRevision || 0,
      confirmed: true,
    })).then(function (r) {
      msg($("scan-msg"), "扫描进度已初始化：" + r.skip_note, "ok");
      $("initialize-btn").classList.add("hidden");
      $("start-panel").classList.add("hidden");
      setEnabled("scan-btn", true);
    }).catch(function (e) { msg($("scan-msg"), e.message, "error"); });
  });

  function resumeRun(run) {
    if (!run || ["succeeded", "partial", "cancelled", "failed", "interrupted"]
        .indexOf(run.status) >= 0) {
      return;
    }
    state.currentRunId = run.run_id;
    $("cancel-btn").classList.remove("hidden");
    $("continue-btn").classList.add("hidden");
    if (run.status === "awaiting_login") {
      $("login-confirm-btn").classList.remove("hidden");
      msg($("auth-msg"), "已有扫描在等待登录确认", "warn");
    }
    msg($("scan-msg"), "检测到正在运行的任务（" + run.kind + "），已重新接入界面", "warn");
    pollRun(run.run_id, function (updated) {
      if (updated.status === "awaiting_login") {
        $("login-confirm-btn").classList.remove("hidden");
        msg($("auth-msg"), "请在已打开的 NGA 页面完成登录，再点击“我已完成登录”", "warn");
      } else if (updated.status === "partial") {
        msg($("scan-msg"), "已处理一批，还有约 " + updated.result.remaining_pages + " 页", "warn");
        $("continue-btn").classList.remove("hidden");
      } else if (updated.status === "succeeded") {
        showScanResult(updated);
      } else if (["failed", "cancelled", "interrupted"].indexOf(updated.status) >= 0) {
        msg($("scan-msg"), (updated.error && updated.error.message) || updated.status, "error");
      }
      if (["succeeded", "partial", "cancelled", "failed", "interrupted"]
          .indexOf(updated.status) >= 0) {
        $("cancel-btn").classList.add("hidden");
        state.currentRunId = null;
      }
    });
  }

  function startScan(dryRun) {
    api("/api/operator/threads/" + state.activeTid + "/status").then(function (st) {
      return api("/api/operator/threads/" + state.activeTid + "/scan", json("POST", {
        dry_run: dryRun, confirm_import: true, expected_state_sha256: st.state_sha256,
      }));
    }).then(function (r) {
      state.currentRunId = r.run_id;
      $("cancel-btn").classList.remove("hidden");
      $("continue-btn").classList.add("hidden");
      msg($("scan-msg"), dryRun ? "正在只读检查（不导入、不推进进度）…" : "扫描中…", "warn");
      pollRun(r.run_id, function (run) {
        if (run.status === "partial") {
          msg($("scan-msg"), "已处理一批，还有约 " + run.result.remaining_pages + " 页", "warn");
          if (!dryRun) { $("continue-btn").classList.remove("hidden"); }
        } else if (run.status === "succeeded") {
          showScanResult(run);
        } else if (["failed", "cancelled", "interrupted"].indexOf(run.status) >= 0) {
          msg($("scan-msg"), (run.error && run.error.message) || run.status, "error");
        }
        if (["succeeded", "partial", "failed", "cancelled", "interrupted"].indexOf(run.status) >= 0) {
          $("cancel-btn").classList.add("hidden");
        }
      });
    }).catch(function (e) { msg($("scan-msg"), e.message, "error"); });
  }

  $("scan-btn").addEventListener("click", function () {
    startScan(false);
  });

  $("scan-dry-btn").addEventListener("click", function () {
    startScan(true);
  });

  $("continue-btn").addEventListener("click", function () {
    startScan(false);
  });

  $("cancel-btn").addEventListener("click", function () {
    api("/api/operator/runs/" + state.currentRunId, json("POST", {}))
      .then(function () { msg($("scan-msg"), "已请求取消", "warn"); })
      .catch(function (e) { msg($("scan-msg"), e.message, "error"); });
  });

  function showScanResult(run) {    $("run-result-panel").classList.remove("hidden");
    var r = run.result || {};
    state.resultTid = run.tid || null;
    var parts = [
      "读取页 " + r.pages_scanned + "，楼层 " + r.floors_seen,
      "讨论 " + r.discussion_count,
      "标记投稿 " + r.triggered_count,
      "新进待审 " + r.imported_count,
      "待补全 " + r.incomplete_count,
      "更新待复查 " + r.updated_count,
      "已见跳过 " + (r.known_skip_count || 0),
      "早于日期 " + r.date_excluded_count,
      "时间待确认 " + r.date_unresolved_count,
    ];
    msg($("run-result"), parts.join("；"), "ok");
    if (r.triggered_count === 0) {
      var reasons = [];
      if ((r.known_skip_count || 0) > 0) {
        reasons.push("已见投稿 " + r.known_skip_count);
      }
      if ((r.known_baseline_trigger_count || 0) > 0) {
        reasons.push("基线内 [收录] " + r.known_baseline_trigger_count);
      }
      if ((r.known_discussion_skip_count || 0) > 0) {
        reasons.push("已见讨论 " + r.known_discussion_skip_count);
      }
      if ((r.discussion_count || 0) > 0) {
        reasons.push("新讨论 " + r.discussion_count);
      }
      if ((r.date_excluded_count || 0) > 0) {
        reasons.push("早于所选日期 " + r.date_excluded_count);
      }
      if ((r.date_unresolved_count || 0) > 0) {
        reasons.push("时间待确认 " + r.date_unresolved_count);
      }
      var reason;
      if ((r.floors_seen || 0) === 0) {
        reason = "（访问和扫描完成，但这批没有可读楼层）";
      } else if (reasons.length) {
        reason = "（本批未发现新投稿：" + reasons.join("，") + "）";
      } else {
        reason = "（访问和扫描完成，但这批楼层没有 [收录] 投稿）";
      }
      var note = document.createElement("span");
      note.className = "muted";
      note.textContent = reason;
      $("run-result").appendChild(note);
    }
    $("goto-inbox-btn").classList.remove("hidden");
  }

  $("goto-inbox-btn").addEventListener("click", function () {
    if (state.resultTid) { $("filter-tid").value = String(state.resultTid); }
    $("filter-q").value = "";
    $("filter-status").value = "";
    inboxPage = 1;
    showSection("inbox");
  });

  function pollRun(runId, cb) {
    var timer = setInterval(function () {
      api("/api/operator/runs/" + runId).then(function (run) {
        cb(run);
        if (["succeeded", "partial", "cancelled", "failed", "interrupted"].indexOf(run.status) >= 0) {
          clearInterval(timer);
        }
      }).catch(function () { clearInterval(timer); });
    }, 1200);
  }

  /* ---- 待审 ---- */

  var inboxPage = 1;

  $("filter-apply").addEventListener("click", function () {
    dirtyGuard(function () { inboxPage = 1; loadInbox(); });
  });
  $("filter-next").addEventListener("click", function () {
    dirtyGuard(function () { inboxPage += 1; loadInbox(); });
  });
  $("filter-prev").addEventListener("click", function () {
    dirtyGuard(function () { inboxPage = Math.max(1, inboxPage - 1); loadInbox(); });
  });

  function loadInbox() {
    var q = encodeURIComponent($("filter-q").value || "");
    api("/api/operator/items?tid=" + $("filter-tid").value + "&status=" + $("filter-status").value
      + "&q=" + q + "&page=" + inboxPage + "&page_size=50").then(function (data) {
        var body = $("items-body");
        body.textContent = "";
        if (!data.items.length) {
          var empty = document.createElement("tr");
          var cell = document.createElement("td");
          cell.colSpan = 3;
          cell.className = "muted";
          cell.textContent = "没有符合条件的稿件；可切换帖子/状态或先在“帖子与扫描”执行检查新增投稿。";
          empty.appendChild(cell);
          body.appendChild(empty);
        }
        data.items.forEach(function (item) {
          var tr = document.createElement("tr");
          tr.classList.toggle("selected", item.id === state.selectedItemId);
          [item.title || "（无标题）", item.author || "", statusLabel(item.status)].forEach(function (cell) {
            var td = document.createElement("td");
            td.textContent = cell;
            tr.appendChild(td);
          });
          tr.addEventListener("click", function () {
            if (item.id === state.selectedItemId) { return; }
            dirtyGuard(function () { openReview(item.id); });
          });
          body.appendChild(tr);
        });
        $("filter-page").textContent = "第 " + data.page + " 页，共 " + data.total + " 条";
        $("filter-next").classList.toggle("hidden", data.page * data.page_size >= data.total);
        $("filter-prev").classList.toggle("hidden", data.page <= 1);
      }).catch(function (e) { msg($("status-diagnostic"), e.message, "error"); });
  }

  function openReview(itemId) {
    state.selectedItemId = itemId;
    state.previewId = null;
    state.operationId = null;
    state.writerHashes = null;
    state.previewReady = false;
    state.dirty = false;
    state.choicesDirty = false;
    state.editVersion += 1;
    invalidatePreview();
    $("review-panel").classList.add("hidden");
    var requestVersion = ++state.detailRequestVersion;
    api("/api/operator/items/" + itemId).then(function (detail) {
      if (requestVersion !== state.detailRequestVersion) {
        return;  // 迟到响应不得覆盖当前稿件
      }
      function isCurrent() { return requestVersion === state.detailRequestVersion; }
      function refreshPreviewButton() {
        if (previewBtn) { previewBtn.disabled = detail.status !== "approved" || hasUnsaved(); }
      }
  function markEdited(choices) {
    if (!isCurrent()) { return; }
    if (choices) { state.choicesDirty = true; } else { state.dirty = true; }
    state.editVersion += 1;
    invalidatePreview();
    refreshPreviewButton();
    state.syncSaveEnabled();
  }
      var box = $("review-form");
      box.textContent = "";
      $("review-panel").classList.remove("hidden");
      var md = detail.review_fields || {};
      var fields = { title: md.title || "", author: md.author || "", translator: md.translator || "",
        series: md.series || "", tags: (md.tags || []).join(","), body_text: md.body_text || "" };
      var form = document.createElement("div");
      [["title", "标题"], ["author", "作者"], ["translator", "译者"], ["series", "系列"]].forEach(function (pair) {
        var row = document.createElement("div");
        row.className = "field-row";
        var label = document.createElement("label");
        label.textContent = pair[1];
        var input = document.createElement("input");
        input.type = "text";
        input.dataset.field = pair[0];
        input.value = fields[pair[0]];
        input.addEventListener("input", function () { markEdited(false); });
        row.appendChild(label); row.appendChild(input);
        form.appendChild(row);
      });
      var tagsRow = document.createElement("div");
      tagsRow.className = "field-row";
      var tagsLabel = document.createElement("label");
      tagsLabel.textContent = "标签";
      var tagsInput = document.createElement("input");
      tagsInput.type = "text"; tagsInput.dataset.field = "tags"; tagsInput.value = fields.tags;
      tagsInput.addEventListener("input", function () { markEdited(false); });
      tagsRow.appendChild(tagsLabel); tagsRow.appendChild(tagsInput);
      form.appendChild(tagsRow);
      var sourceInputs = {};
      var sourceDetails = document.createElement("details");
      sourceDetails.className = "sources-block";
      var sourceSummary = document.createElement("summary");
      function updateSourceCount() {
        var total = 0;
        ["nga", "original_series", "original", "translation_series", "translation"].forEach(
          function (key) { total += splitLines(sourceInputs[key].value).length; });
        sourceSummary.textContent = "来源（已填 " + total + " 项）";
      }
      sourceDetails.appendChild(sourceSummary);
      [
        ["nga", "NGA 来源（每行一个）"],
        ["original_series", "原作系列（每行一个）"],
        ["original", "原作（每行一个）"],
        ["translation_series", "译作系列（每行一个）"],
        ["translation", "译作（每行一个）"],
      ].forEach(function (pair) {
        var row = document.createElement("div");
        row.className = "field-row";
        var label = document.createElement("label");
        label.textContent = pair[1];
        var area = document.createElement("textarea");
        area.rows = 2;
        area.value = ((md.sources && md.sources[pair[0]]) || []).join("\n");
        area.addEventListener("input", function () {
          markEdited(false);
          updateSourceCount();
        });
        sourceInputs[pair[0]] = area;
        row.appendChild(label); row.appendChild(area); sourceDetails.appendChild(row);
      });
      updateSourceCount();
      form.appendChild(sourceDetails);
      var bodyRow = document.createElement("div");
      bodyRow.className = "field-row";
      var bodyLabel = document.createElement("label");
      bodyLabel.textContent = "正文";
      var bodyInput = document.createElement("textarea");
      bodyInput.rows = 8; bodyInput.dataset.field = "body_text"; bodyInput.value = fields.body_text;
      bodyInput.addEventListener("input", function () { markEdited(false); });
      bodyRow.appendChild(bodyLabel); bodyRow.appendChild(bodyInput);
      form.appendChild(bodyRow);
      var rawDetails = document.createElement("details");
      var rawSummary = document.createElement("summary");
      rawSummary.textContent = "查看原始楼层（只读）";
      var raw = document.createElement("pre");
      raw.textContent = ((detail.original_draft.content || {}).raw_text) || "";
      rawDetails.appendChild(rawSummary); rawDetails.appendChild(raw);
      form.appendChild(rawDetails);
      if (detail.origin && detail.origin.canonical_url) {
        var originLink = document.createElement("a");
        originLink.href = detail.origin.canonical_url;
        originLink.target = "_blank";
        originLink.rel = "noopener noreferrer";
        originLink.textContent = "在 NGA 打开原楼层";
        form.appendChild(originLink);
      }
      var diagnostics = document.createElement("div");
      diagnostics.className = "review-diagnostics";
      var missing = (detail.diagnostics && detail.diagnostics.missing_fields) || [];
      var warnings = (detail.diagnostics && detail.diagnostics.warnings) || [];
      var unknown = (detail.diagnostics && detail.diagnostics.unknown_fields) || [];
      diagnostics.textContent = "待补字段：" + (missing.join("、") || "无")
        + "；警告：" + (warnings.map(function (w) { return w.code; }).join("、") || "无")
        + "；未知字段：" + (unknown.map(function (u) { return u.name; }).join("、") || "无");
      form.appendChild(diagnostics);
      if ((detail.duplicates || []).length) {
        var duplicates = document.createElement("div");
        duplicates.className = "warn";
        duplicates.textContent = "正文完全相同的候选：" + detail.duplicates.map(function (d) {
          return "#" + d.id + " " + (d.title || "无标题") + "（" + statusLabel(d.status) + "）";
        }).join("；");
        var dupList = document.createElement("div");
        (detail.duplicates || []).forEach(function (d) {
          var row = document.createElement("div");
          row.textContent = "#" + d.id + " " + (d.title || "无标题") + "（" + statusLabel(d.status) + "）";
          if (d.id !== itemId) {
            var dupBtn = document.createElement("button");
            dupBtn.textContent = "本条与 #" + d.id + " 重复，忽略本条";
            dupBtn.addEventListener("click", function () {
              if (state.dirty) { msg($("status-diagnostic"), "请先保存修改", "warn"); return; }
              var reason = window.prompt("重复处置理由（必填，将写入审计）：",
                                         "疑似与 #" + d.id + " 重复");
              if (reason === null) { return; }
              if (!reason.trim()) { msg($("status-diagnostic"), "重复处置必须填写理由", "error"); return; }
              doTransition("ignored", reason.trim());
            });
            row.appendChild(dupBtn);
            var openDup = document.createElement("button");
            openDup.textContent = "打开该稿对照";
            openDup.addEventListener("click", function () { openReview(d.id); });
            row.appendChild(openDup);
          }
          dupList.appendChild(row);
        });
        duplicates.appendChild(dupList);
        form.appendChild(duplicates);
      }
      // 同楼历史版本对照（G03）：按 tid+pid 关联前后稿，只读比对，人工决定
      if ((detail.siblings || []).length) {
        var siblingsBox = document.createElement("details");
        var siblingSummary = document.createElement("summary");
        siblingSummary.textContent = "同楼历史版本对照（" + detail.siblings.length + " 条，只读）";
        siblingsBox.appendChild(siblingSummary);
        detail.siblings.forEach(function (sib) {
          var row = document.createElement("div");
          row.className = "sibling-row";
          row.textContent = "#" + sib.id + " " + (sib.title || "无标题")
            + "（" + statusLabel(sib.status) + "）";
          var openSibling = document.createElement("button");
          openSibling.textContent = "打开此稿";
          openSibling.addEventListener("click", function () { openReview(sib.id); });
          row.appendChild(openSibling);
          var compareToggle = document.createElement("button");
          compareToggle.textContent = "与当前正文对照";
          var compareBox = document.createElement("div");
          compareBox.className = "hidden";
          compareToggle.addEventListener("click", function () {
            compareBox.classList.toggle("hidden");
          });
          var labelA = document.createElement("div");
          labelA.textContent = "该版本正文（只读）：";
          var preA = document.createElement("pre");
          preA.textContent = sib.original_body || sib.body_text || "";
          var labelB = document.createElement("div");
          labelB.textContent = "当前正文（只读）：";
          var preB = document.createElement("pre");
          preB.textContent = md.body_text || "";
          compareBox.appendChild(labelA);
          compareBox.appendChild(preA);
          compareBox.appendChild(labelB);
          compareBox.appendChild(preB);
          row.appendChild(compareToggle);
          row.appendChild(compareBox);
          siblingsBox.appendChild(row);
        });
        form.appendChild(siblingsBox);
      }
      // 版本与审计（只读）：消费既有 history，不做自动合并/覆盖
      if ((detail.history || []).length) {
        var historyDetails = document.createElement("details");
        var historySummary = document.createElement("summary");
        historySummary.textContent = "版本与审计（" + detail.history.length + " 条，只读）";
        historyDetails.appendChild(historySummary);
        detail.history.forEach(function (entry) {
          var line = document.createElement("div");
          line.textContent = (entry.changed_at || "").replace("T", " ").slice(0, 19)
            + "：" + statusLabel(entry.from_status) + " → " + statusLabel(entry.to_status)
            + "（" + (entry.reason || "无理由") + "）";
          historyDetails.appendChild(line);
        });
        form.appendChild(historyDetails);
      }
      // 原/新正文对照（只读）：仅当当前工作稿与原始楼层不同
      var originalBody = ((detail.original_draft.content || {}).raw_text)
        || ((detail.original_draft.content || {}).body_text) || "";
      if (originalBody && md.body_text !== undefined && md.body_text !== originalBody) {
        var compareDetails = document.createElement("details");
        var compareSummary = document.createElement("summary");
        compareSummary.textContent = "原/新正文对照（只读）";
        compareDetails.appendChild(compareSummary);
        var originalTitle = document.createElement("div");
        originalTitle.textContent = "原始楼层：";
        var originalPre = document.createElement("pre");
        originalPre.textContent = originalBody;
        var currentTitle = document.createElement("div");
        currentTitle.textContent = "当前工作稿：";
        var currentPre = document.createElement("pre");
        currentPre.textContent = md.body_text;
        compareDetails.appendChild(originalTitle);
        compareDetails.appendChild(originalPre);
        compareDetails.appendChild(currentTitle);
        compareDetails.appendChild(currentPre);
        form.appendChild(compareDetails);
      }

      var normalizeSourcesBtn = document.createElement("button");
      normalizeSourcesBtn.textContent = "检查并规范化来源链接";
      normalizeSourcesBtn.addEventListener("click", function () {
        var version = state.editVersion;
        var keys = ["nga", "original_series", "original", "translation_series", "translation"];
        var refs = [];
        var urls = [];
        keys.forEach(function (key) {
          splitLines(sourceInputs[key].value).forEach(function (url) {
            refs.push(key); urls.push(url);
          });
        });
        api("/api/operator/items/" + itemId + "/source-preview", json("POST", {
          urls: urls,
        })).then(function (preview) {
          if (!isCurrent() || version !== state.editVersion) { return; }
          var grouped = { nga: [], original_series: [], original: [], translation_series: [], translation: [] };
          preview.results.forEach(function (result, index) {
            grouped[refs[index]].push(result.normalized || result.original);
          });
          keys.forEach(function (key) { sourceInputs[key].value = grouped[key].join("\n"); });
          markEdited(false);
          var problems = preview.results.reduce(function (sum, result) {
            return sum + result.warnings.length + result.blockers.length;
          }, 0);
          msg($("status-diagnostic"), problems
            ? "来源已规范化，但仍有 " + problems + " 项提示，请检查"
            : "来源链接已规范化；请保存修改", problems ? "warn" : "ok");
        }).catch(function (e) { msg($("status-diagnostic"), e.message, "error"); });
      });
      form.appendChild(normalizeSourcesBtn);

      var actions = document.createElement("div");
      actions.className = "action-bar";
      var saveBtn = document.createElement("button");
      saveBtn.textContent = "保存修改";
      saveBtn.className = "primary";
      saveBtn.addEventListener("click", function () {
        if (detail.status === "approved") {
          msg($("status-diagnostic"),
              "已批准稿不可直接修改：请先点“退回待审”，修改并保存后重新批准", "warn");
          return;
        }
        var version = state.editVersion;
        invalidatePreview();
        saveBtn.disabled = true;
        var payload = {
          title: form.querySelector("[data-field=title]").value,
          author: form.querySelector("[data-field=author]").value,
          translator: form.querySelector("[data-field=translator]").value || null,
          series: form.querySelector("[data-field=series]").value || null,
          tags: (form.querySelector("[data-field=tags]").value || "").split(/[,，]/).map(function (s) { return s.trim(); }).filter(Boolean),
          sources: {
            nga: splitLines(sourceInputs.nga.value),
            original_series: splitLines(sourceInputs.original_series.value),
            original: splitLines(sourceInputs.original.value),
            translation_series: splitLines(sourceInputs.translation_series.value),
            translation: splitLines(sourceInputs.translation.value),
          },
          body_text: form.querySelector("[data-field=body_text]").value,
        };
        api("/api/operator/items/" + itemId + "/review", json("PUT", payload)).then(function (result) {
          if (!isCurrent()) { return; }
          if (version !== state.editVersion) {
            msg($("status-diagnostic"), "已保存提交时的内容；后续修改仍未保存", "warn");
            return;
          }
          var tagsChanged = JSON.stringify([md.tags, md.series]) !== JSON.stringify([payload.tags, payload.series]);
          md = payload;
          detail.status = result.status;
          applyEditableState(result.status);
          state.dirty = false;
          state.syncSaveEnabled();
          if (tagsChanged) {
            state.choicesDirty = true;
            if (tagCatalog) { renderTagChoices(tagCatalog); }
          }
          refreshPreviewButton();
          msg($("status-diagnostic"), "已保存", "ok");
        }).catch(function (e) {
          if (isCurrent()) { msg($("status-diagnostic"), e.message, "error"); }
        }).finally(function () { saveBtn.disabled = false; });
      });
      var approveBtn = document.createElement("button");
      approveBtn.textContent = "批准稿件";
      approveBtn.addEventListener("click", function () {
        if (state.dirty) { msg($("status-diagnostic"), "请先保存修改", "warn"); return; }
        invalidatePreview();
        approveBtn.disabled = true;
        api("/api/operator/items/" + itemId + "/transition", json("POST", {
          to_status: "approved", reason: "审核通过",
        })).then(function () {
          if (!isCurrent()) { return; }
          detail.status = "approved";
          applyEditableState("approved");
          refreshPreviewButton();
          previewBtn.title = "";
          msg($("status-diagnostic"), "已批准，可以保存人工决定并生成预览", "ok");
        })
          .catch(function (e) { if (isCurrent()) { msg($("status-diagnostic"), e.message, "error"); } })
          .finally(function () { approveBtn.disabled = false; });
      });
      function doTransition(toStatus, reason) {
        invalidatePreview();
        api("/api/operator/items/" + itemId + "/transition", json("POST", {
          to_status: toStatus, reason: reason,
        })).then(function (result) {
          if (!isCurrent()) { return; }
          detail.status = result.status;
          applyEditableState(result.status);
          refreshPreviewButton();
          msg($("status-diagnostic"),
            "状态已更新：" + statusLabel(result.status)
            + (result.status === "candidate" ? "（可继续修改后重新批准）" : ""), "ok");
        }).catch(function (e) {
          if (isCurrent()) { msg($("status-diagnostic"), e.message, "error"); }
        });
      }
      var ignoreBtn = document.createElement("button");
      ignoreBtn.textContent = "忽略稿件";
      ignoreBtn.addEventListener("click", function () {
        if (state.dirty) { msg($("status-diagnostic"), "请先保存修改", "warn"); return; }
        var reason = window.prompt("填写忽略理由（必填，将写入审计）：", "");
        if (reason === null) { return; }
        if (!reason.trim()) { msg($("status-diagnostic"), "忽略必须填写理由", "error"); return; }
        doTransition("ignored", reason.trim());
      });
      var returnBtn = document.createElement("button");
      returnBtn.textContent = "退回待审";
      returnBtn.addEventListener("click", function () {
        if (state.dirty) { msg($("status-diagnostic"), "请先保存修改", "warn"); return; }
        var reason = window.prompt("填写退回理由（必填，将写入审计）：", "需要继续修改");
        if (reason === null) { return; }
        if (!reason.trim()) { msg($("status-diagnostic"), "退回必须填写理由", "error"); return; }
        doTransition("candidate", reason.trim());
      });
      actions.appendChild(saveBtn); actions.appendChild(approveBtn);
      actions.appendChild(ignoreBtn); actions.appendChild(returnBtn);
      form.appendChild(actions);
      var lockedHint = document.createElement("div");
      lockedHint.className = "warn hidden";
      form.appendChild(lockedHint);
      var auditControls = Array.prototype.slice.call(form.querySelectorAll("[data-field]"));
      ["nga", "original_series", "original", "translation_series", "translation"].forEach(
        function (key) { auditControls.push(sourceInputs[key]); });
      function setAuditReadOnly(readonly) {
        auditControls.forEach(function (control) { control.readOnly = readonly; });
        if (normalizeSourcesBtn) { normalizeSourcesBtn.disabled = readonly; }
      }
      function applyEditableState(status) {
        var editable = status === "candidate" || status === "incomplete"
          || status === "suspected_duplicate";
        var exported = status === "exported";
        var approved = status === "approved";
        // 已批准稿的审核字段明确只读：先“退回待审”再编辑，避免
        // 保存与退回互相阻断的死路（输入内容保留在页面上）。
        setAuditReadOnly(approved || exported);
        state.syncSaveEnabled = function () {
          if (exported) { saveBtn.disabled = true; return; }
          saveBtn.disabled = !(editable || (approved && state.dirty));
        };
        state.syncSaveEnabled();
        approveBtn.disabled = !editable;
        ignoreBtn.disabled = !editable;
        returnBtn.classList.toggle("hidden", editable || exported);
        lockedHint.classList.toggle("hidden", !(exported || approved));
        if (exported) {
          lockedHint.textContent = "该稿已写入候选数据库，内容不可直接修改；如需修订请在交付中心基于新候选重新处理。";
        } else if (approved) {
          lockedHint.textContent = "已批准：审核字段只读；如需修改请先点“退回待审”，退回后再编辑、保存并重新批准。";
        }
        if (!editable && !exported && !approved) {
          msg($("status-diagnostic"), "当前状态（" + statusLabel(status)
            + "）不可直接编辑；如需修改请点“退回待审”。", "warn");
        }
      }
      applyEditableState(detail.status);

      var writerSection = document.createElement("div");
      writerSection.className = "writer-decisions";
      var decisionsTitle = document.createElement("h3");
      decisionsTitle.textContent = "候选数据库人工决定";
      writerSection.appendChild(decisionsTitle);
      var decisionMsg = document.createElement("div");
      decisionMsg.className = "msg";
      writerSection.appendChild(decisionMsg);
      var uploadLabel = document.createElement("label");
      uploadLabel.textContent = "录入时间 ";
      var uploadInput = document.createElement("input");
      uploadInput.type = "datetime-local";
      uploadInput.value = epochToLocalInput(detail.choices && detail.choices.upload_time_seconds);
      uploadLabel.appendChild(uploadInput);
      writerSection.appendChild(uploadLabel);
      var noteLabel = document.createElement("label");
      noteLabel.textContent = "备注 ";
      var noteInput = document.createElement("input");
      noteInput.type = "text";
      noteInput.value = (detail.choices && detail.choices.note) || "";
      noteLabel.appendChild(noteInput);
      writerSection.appendChild(noteLabel);
      var contentMode = document.createElement("select");
      [["plain", "纯文本"], ["nga_ubb", "保留基础 NGA 格式"]].forEach(function (entry) {
        var option = document.createElement("option");
        option.value = entry[0]; option.textContent = entry[1];
        contentMode.appendChild(option);
      });
      contentMode.value = (detail.choices && detail.choices.content_mode) || "plain";
      writerSection.appendChild(contentMode);
      var formattedBody = document.createElement("textarea");
      formattedBody.rows = 5;
      formattedBody.placeholder = "采用 NGA 格式时，在这里检查/修改格式正文";
      formattedBody.value = (detail.choices && detail.choices.formatted_body) || fields.body_text;
      formattedBody.classList.toggle("hidden", contentMode.value !== "nga_ubb");
      contentMode.addEventListener("change", function () {
        formattedBody.classList.toggle("hidden", contentMode.value !== "nga_ubb");
      });
      writerSection.appendChild(formattedBody);
      var extractFormatBtn = document.createElement("button");
      extractFormatBtn.textContent = "从原始楼层提取基础格式";
      extractFormatBtn.addEventListener("click", function () {
        var version = state.editVersion;
        api("/api/operator/items/" + itemId + "/format-preview", json("POST", {
          formatted_body: null,
        })).then(function (preview) {
          if (!isCurrent() || version !== state.editVersion) { return; }
          if (!preview.formatted_body) {
            var codes = (preview.diagnostics || []).map(function (d) { return d.code; }).join(", ");
            msg(decisionMsg, "无法自动提取格式" + (codes ? "：" + codes : ""), "warn");
            return;
          }
          markEdited(true);
          contentMode.value = "nga_ubb";
          formattedBody.value = preview.formatted_body;
          formattedBody.classList.remove("hidden");
          msg(decisionMsg, "已从原始楼层提取基础 UBB，请人工检查", "ok");
        }).catch(function (e) { msg(decisionMsg, e.message, "error"); });
      });
      writerSection.appendChild(extractFormatBtn);
      var plainLossCheck = document.createElement("input");
      plainLossCheck.type = "checkbox";
      plainLossCheck.checked = Boolean(detail.choices && detail.choices.plain_loss_confirmed);
      var plainLossLabel = document.createElement("label");
      plainLossLabel.appendChild(plainLossCheck);
      plainLossLabel.appendChild(document.createTextNode(" 我确认选择纯文本时可能丢失颜色/粗体等格式"));
      writerSection.appendChild(plainLossLabel);
      var tagDecisionBox = document.createElement("div");
      tagDecisionBox.textContent = "正在加载标签目录…";
      writerSection.appendChild(tagDecisionBox);
      var choiceRows = [];
      var tagCatalog = null;
      function renderTagChoices(catalog) {
        var previous = {};
        choiceRows.forEach(function (row) { previous[JSON.stringify(row.request)] = row.select.value; });
        choiceRows = [];
        tagDecisionBox.textContent = "";
        var requested = (md.tags || []).map(function (value) {
          return { kind: "tag", value: value };
        });
        if (md.series) { requested.push({ kind: "series", value: md.series }); }
        requested.forEach(function (request) {
          var row = document.createElement("div");
          row.className = "tag-decision";
          var label = document.createElement("span");
          label.textContent = (request.kind === "series" ? "系列：" : "标签：") + request.value + " ";
          var select = document.createElement("select");
          var prompt = document.createElement("option");
          prompt.value = ""; prompt.textContent = "请选择（唯一同名时可自动复用）";
          select.appendChild(prompt);
          // docs/01 合同词表：0 其他、1 角色、2 系列、3 长篇/合集、4 争议/不适
          var typeLabels = {
            0: "其他", 1: "角色", 2: "系列", 3: "长篇/合集", 4: "争议/不适",
          };
          var exact = catalog.tags.filter(function (tag) { return tag.name === request.value; });
          exact.forEach(function (tag) {
            var option = document.createElement("option");
            option.value = "existing:" + tag.id;
            option.textContent = "复用 ID " + tag.id + "（类型 " + tag.type + "："
              + (typeLabels[tag.type] || "未知") + "）";
            select.appendChild(option);
          });
          [0, 1, 2, 3, 4].forEach(function (type) {
            var option = document.createElement("option");
            option.value = "new:" + type;
            option.textContent = "新建，类型 " + type + "（" + typeLabels[type] + "）";
            select.appendChild(option);
          });
          var saved = ((detail.choices && detail.choices.tag_choices) || []).find(function (choice) {
            return choice.requested_kind === request.kind && choice.requested_value === request.value;
          });
          if (saved) {
            if (saved.existing_tag_id !== null) { select.value = "existing:" + saved.existing_tag_id; }
            else if (saved.new_tag_type !== null) { select.value = "new:" + saved.new_tag_type; }
          }
          if (Object.prototype.hasOwnProperty.call(previous, JSON.stringify(request))) {
            select.value = previous[JSON.stringify(request)];
          }
          row.appendChild(label); row.appendChild(select); tagDecisionBox.appendChild(row);
          choiceRows.push({ request: request, select: select, exact: exact });
        });
        if (!requested.length) { tagDecisionBox.textContent = "没有待决定的标签或系列"; }
      }
      api("/api/operator/catalog").then(function (catalogResponse) {
        if (!isCurrent()) { return; }
        tagCatalog = catalogResponse.catalog || { tags: [] };
        renderTagChoices(tagCatalog);
      }).catch(function (e) { msg(decisionMsg, e.message, "error"); });
      var saveChoicesBtn = document.createElement("button");
      saveChoicesBtn.textContent = "保存人工决定";
      saveChoicesBtn.addEventListener("click", function () {
        if (state.dirty) { msg(decisionMsg, detail.status === "approved"
          ? "审核字段有未保存修改；已批准稿请先点“退回待审”再保存修改"
          : "请先保存稿件修改，再保存人工决定", "warn"); return; }
        if (!tagCatalog) { msg(decisionMsg, "标签目录尚未加载完成", "warn"); return; }
        var seconds = localInputToEpoch(uploadInput.value);
        if (seconds === null) { msg(decisionMsg, "请选择有效录入时间", "error"); return; }
        var orderedSources = [];
        ["nga", "original_series", "original", "translation_series", "translation"].forEach(function (key) {
          splitLines(sourceInputs[key].value).forEach(function (url) { orderedSources.push(url); });
        });
        var unresolved = choiceRows.filter(function (row) {
          return row.select.value === "" && row.exact.length !== 1;
        });
        if (unresolved.length) {
          msg(decisionMsg, "请为所有零匹配或多重同名的标签/系列作出明确选择", "error");
          return;
        }
        var tagChoices = choiceRows.map(function (row) {
          var parts = row.select.value.split(":");
          return { requested_kind: row.request.kind, requested_value: row.request.value,
            existing_tag_id: parts[0] === "existing" ? parseInt(parts[1], 10)
              : (row.select.value === "" && row.exact.length === 1 ? row.exact[0].id : null),
            new_tag_type: parts[0] === "new" ? parseInt(parts[1], 10) : null };
        });
        var version = state.editVersion;
        invalidatePreview();
        saveChoicesBtn.disabled = true;
        api("/api/operator/items/" + itemId + "/choices", json("PUT", {
          note: noteInput.value || null, upload_time_seconds: seconds,
          ordered_sources: orderedSources, tag_choices: tagChoices,
          content_mode: contentMode.value,
          formatted_body: contentMode.value === "nga_ubb" ? formattedBody.value : null,
          plain_loss_confirmed: plainLossCheck.checked,
        })).then(function () {
          if (!isCurrent()) { return; }
          if (version !== state.editVersion) {
            msg(decisionMsg, "已保存提交时的人工决定；后续修改仍未保存", "warn");
            return;
          }
          state.choicesDirty = false;
          refreshPreviewButton();
          msg(decisionMsg, "人工决定已保存；请重新生成预览", "ok");
        })
          .catch(function (e) { msg(decisionMsg, e.message, "error"); })
          .finally(function () { saveChoicesBtn.disabled = false; });
      });
      writerSection.appendChild(saveChoicesBtn);
      var previewBtn = document.createElement("button");
      previewBtn.textContent = "生成并查看预览";
      previewBtn.disabled = detail.status !== "approved";
      if (previewBtn.disabled) { previewBtn.title = "请先保存并批准稿件"; }
      var previewDisplay = document.createElement("div");
      previewDisplay.className = "writer-preview-inline hidden";
      previewBtn.addEventListener("click", function () {
        doWriterPreview(itemId, previewDisplay, writeBtn);
      });
      writerSection.appendChild(previewBtn);
      writerSection.appendChild(previewDisplay);
      var writerMsg = document.createElement("div");
      writerMsg.className = "msg";
      writerSection.appendChild(writerMsg);
      var confirmCheck = document.createElement("input");
      confirmCheck.type = "checkbox";
      confirmCheck.className = "writer-confirm";
      var confirmLabel = document.createElement("label");
      confirmLabel.appendChild(confirmCheck);
      confirmLabel.appendChild(document.createTextNode(" 我已确认大书库已关闭"));
      writerSection.appendChild(confirmLabel);
      var writeBtn = document.createElement("button");
      writeBtn.textContent = "确认生成候选数据库";
      writeBtn.className = "primary writer-write";
      writeBtn.disabled = true;
      writeBtn.addEventListener("click", function () {
        if (!isCurrent() || hasUnsaved()) { return; }
        if (!state.previewId) { msg(writerMsg, "请先点击生成预览", "warn"); return; }
        if (!state.previewReady) { msg(writerMsg, "预览仍有阻断项，不能生成候选", "error"); return; }
        if (!confirmCheck.checked) { msg(writerMsg, "请先确认大书库已关闭", "warn"); return; }
        writeBtn.disabled = true;
        api("/api/operator/items/" + itemId + "/writer/write", json("POST", {
          preview_id: state.previewId, app_closed_confirmed: true,
        })).then(function (result) {
          if (!isCurrent()) { return; }
          invalidatePreview();
          detail.status = "exported";
          refreshPreviewButton();
          msg(writerMsg, "候选已生成；正式库未被替换。候选库当前共有 " +
            (result.delivery && result.delivery.count_after) + " 篇文章", "ok");
        }).catch(function (e) { msg(writerMsg, e.message, "error"); });
      });
      writerSection.appendChild(writeBtn);
      writerSection.addEventListener("input", function (event) {
        if (event.target !== confirmCheck) { markEdited(true); }
      });
      writerSection.addEventListener("change", function (event) {
        if (event.target !== confirmCheck) { markEdited(true); }
      });
      form.appendChild(writerSection);

      box.appendChild(form);
    }).catch(function (e) { msg($("status-diagnostic"), e.message, "error"); });
  }

  function doWriterPreview(itemId, previewDisplay, writeBtn) {
    if (itemId !== state.selectedItemId || hasUnsaved()) { return; }
    invalidatePreview();
    var version = state.editVersion;
    var detailVersion = state.detailRequestVersion;
    var previewVersion = state.previewRequestVersion;
    var opName = "candidate-" + newOpId() + ".db";
    api("/api/operator/items/" + itemId + "/writer/preview", json("POST", {
      candidate_file_name: opName,
    })).then(function (r) {
      if (detailVersion !== state.detailRequestVersion || version !== state.editVersion
          || previewVersion !== state.previewRequestVersion || hasUnsaved()) { return; }
      state.previewId = r.preview_id;
      state.operationId = r.operation_id;
      state.writerHashes = r;
      var blockers = (r.preview.diagnostics && r.preview.diagnostics.blockers) || [];
      state.previewReady = Boolean(r.preview.ready_to_write) && blockers.length === 0;
      writeBtn.disabled = !state.previewReady;
      previewDisplay.textContent = "";
      previewDisplay.classList.remove("hidden");
      var plan = r.plan || {};
      var article = plan.article || {};
      var heading = document.createElement("h4");
      heading.textContent = "最终写入预览（尚未创建数据库）";
      previewDisplay.appendChild(heading);
      [["标题", article.name], ["作者", article.author],
        ["译者", article.translator || "无"],
        ["录入时间（Unix 秒）", article.upload_time],
        ["来源", article.source || "无"]].forEach(function (entry) {
        var line = document.createElement("div");
        line.textContent = entry[0] + "：" + entry[1];
        previewDisplay.appendChild(line);
      });
      var contentTitle = document.createElement("div");
      contentTitle.textContent = "最终正文：";
      previewDisplay.appendChild(contentTitle);
      var content = document.createElement("pre");
      content.textContent = article.content || "";
      previewDisplay.appendChild(content);
      var renderedLabel = document.createElement("div");
      renderedLabel.textContent = "实际显示效果（隔离只读，未写库）：";
      previewDisplay.appendChild(renderedLabel);
      var renderedBox = document.createElement("div");
      renderedBox.className = "preview-render";
      renderQuillSafely(renderedBox, article.content || "");
      previewDisplay.appendChild(renderedBox);
      var tags = document.createElement("div");
      tags.textContent = "复用标签：" + JSON.stringify((plan.tags || {}).existing || [])
        + "；新建标签：" + JSON.stringify((plan.tags || {}).create || []);
      previewDisplay.appendChild(tags);
      if (blockers.length) {
        var blocked = document.createElement("div");
        blocked.className = "error";
        blocked.textContent = "阻断项：" + blockers.map(function (b) {
          return b.message || b.code;
        }).join("；");
        previewDisplay.appendChild(blocked);
      }
      msg($("status-diagnostic"), blockers.length
        ? "预览存在阻断项：" + blockers.map(function (b) { return b.code; }).join(", ")
        : "预览就绪，可确认生成候选", blockers.length ? "error" : "ok");
    }).catch(function (e) { msg($("status-diagnostic"), e.message, "error"); });
  }

  function newOpId() {
    function p() { return Math.random().toString(16).slice(2, 6); }
    return p() + p() + "-" + p() + "-4" + p().slice(1) + "-8" + p().slice(1) + "-" + p() + p() + p();
  }

  /* ---- 交付 ---- */

  function loadDeliveries() {
    api("/api/operator/deliveries").then(function (data) {
      var box = $("deliveries");
      box.textContent = "";
      if (!data.deliveries.length) {
        box.textContent = "暂无交付记录";
        return;
      }
      data.deliveries.forEach(function (d) {
        var row = document.createElement("div");
        row.textContent = (d.latest ? "★ 最新 " : "") + d.candidate_file_name
          + "（库内共 " + d.count_after + " 篇，SHA " + d.candidate_sha256.slice(0, 12) + "…）";
        var path = document.createElement("div");
        path.className = "muted";
        path.textContent = d.candidate_path;
        row.appendChild(path);
        var copyPath = document.createElement("button");
        copyPath.textContent = "复制候选路径";
        copyPath.addEventListener("click", function () {
          navigator.clipboard.writeText(d.candidate_path).then(function () {
            msg($("status-diagnostic"), "候选路径已复制", "ok");
          }).catch(function () { msg($("status-diagnostic"), "浏览器不允许复制，请手工选择上方路径", "warn"); });
        });
        row.appendChild(copyPath);
        var viewCopy = document.createElement("button");
        viewCopy.textContent = "生成单独查看副本";
        viewCopy.addEventListener("click", function () {
          api("/api/operator/deliveries/" + d.operation_id + "/view-copy", json("POST", {
            confirmed: true,
          })).then(function (result) {
            msg($("status-diagnostic"), "查看副本已生成：" + result.view_copy_path, "ok");
          }).catch(function (e) { msg($("status-diagnostic"), e.message, "error"); });
        });
        row.appendChild(viewCopy);
        var openFolder = document.createElement("button");
        openFolder.textContent = "打开所在文件夹";
        openFolder.addEventListener("click", function () {
          api("/api/operator/deliveries/" + d.operation_id + "/open-folder", json("POST", {
            confirmed: true,
          })).catch(function (e) { msg($("status-diagnostic"), e.message, "error"); });
        });
        row.appendChild(openFolder);
        box.appendChild(row);
      });
    });
    api("/api/operator/checkpoints").then(function (data) {
      var box = $("checkpoints");
      box.textContent = "";
      data.checkpoints.forEach(function (cp) {
        var row = document.createElement("div");
        row.textContent = cp.checkpoint_id + " revision=" + cp.revision;
        var restore = document.createElement("button");
        restore.textContent = "恢复为新的工作区";
        restore.addEventListener("click", function () {
          if (!window.confirm("恢复会创建一个新的工作区，不覆盖当前工作区。继续吗？")) { return; }
          api("/api/operator/checkpoints/" + cp.checkpoint_id + "/restore", json("POST", {
            confirmed: true,
          })).then(function (result) {
            msg($("status-diagnostic"), "已恢复为新工作区 " + result.new_workspace_id
              + "；请退出并重新启动助手", "ok");
          }).catch(function (e) { msg($("status-diagnostic"), e.message, "error"); });
        });
        row.appendChild(restore);
        box.appendChild(row);
      });
    });
  }

  $("checkpoint-create-btn").addEventListener("click", function () {
    api("/api/operator/checkpoints", json("POST", { confirmed: true }))
      .then(function () { loadDeliveries(); })
      .catch(function (e) { msg($("status-diagnostic"), e.message, "error"); });
  });

  /* ---- 补查历史（R08） ---- */

  function currentRescanKind() {
    return document.querySelector('input[name="rescan-kind"]:checked').value;
  }

  function updateRescanCost() {
    var cost = $("rescan-cost");
    if (currentRescanKind() === "pages") {
      var from = parseInt($("rescan-from").value, 10);
      var to = parseInt($("rescan-to").value, 10);
      cost.textContent = (from > 0 && to >= from)
        ? "将补查 " + (to - from + 1) + " 页（每批最多 10 页，可分批继续；不改变日常进度）"
        : "请填写有效的起止页";
    } else {
      cost.textContent = $("rescan-date").value
        ? "将从第 1 页逐页筛选到当前尾页；补入此前被日期排除的稿件，可分批继续"
        : "请选择较早日期";
    }
  }

  document.querySelectorAll('input[name="rescan-kind"]').forEach(function (r) {
    r.addEventListener("change", function () {
      $("rescan-pages-input").classList.toggle("hidden", r.value !== "pages");
      $("rescan-date-input").classList.toggle("hidden", r.value !== "date");
      updateRescanCost();
    });
  });
  $("rescan-from").addEventListener("input", updateRescanCost);
  $("rescan-to").addEventListener("input", updateRescanCost);
  $("rescan-date").addEventListener("change", updateRescanCost);

  function startRescan(isContinue) {
    api("/api/operator/threads/" + state.activeTid + "/status").then(function (st) {
      var active = state.activeRescan;
      var kind = isContinue && active ? active.kind : currentRescanKind();
      var payload = { confirmed: true, kind: kind, expected_state_sha256: st.state_sha256 };
      if (isContinue && active) {
        // 继续：严格按持久 active_rescan 的原 kind/日期/范围/rescan_id，不从表单取新值
        if (active.rescan_id) { payload.rescan_id = active.rescan_id; }
        if (kind === "pages") {
          payload.from_page = active.from_page;
          payload.to_page = active.to_page;
        } else if (active.lower_bound_utc) {
          payload.lower_bound_date = utcIsoToLocalDate(active.lower_bound_utc);
        }
      } else {
        if (kind === "pages") {
          payload.from_page = parseInt($("rescan-from").value, 10);
          payload.to_page = parseInt($("rescan-to").value, 10);
        } else {
          payload.lower_bound_date = $("rescan-date").value;
        }
        if (active) {
          var sameRange = (kind === active.kind) && (kind === "pages"
            ? (payload.from_page === active.from_page && payload.to_page === active.to_page)
            : (payload.lower_bound_date === utcIsoToLocalDate(active.lower_bound_utc)));
          if (!sameRange && !window.confirm(
              "当前有未完成的补查；将按新范围启动新的补查，原补查进度由新范围替代。继续吗？")) {
            return;
          }
          if (sameRange && !window.confirm(
              "与未完成补查范围相同，将从中断处继续。继续吗？")) {
            return;
          }
        } else if (!window.confirm(
            "补查会读取历史页并补入此前被排除的稿件，但不会改变日常扫描进度。继续吗？")) {
          return;
        }
      }
      return api("/api/operator/threads/" + state.activeTid + "/rescan", json("POST", payload))
        .then(function (r) {
          state.currentRunId = r.run_id;
          $("rescan-cancel-btn").classList.remove("hidden");
          $("rescan-continue-btn").classList.add("hidden");
          msg($("rescan-msg"), isContinue ? "按原范围继续补查…" : "补查中…", "warn");
          pollRun(r.run_id, function (run) {
            if (run.status === "partial") {
              msg($("rescan-msg"), "补查已处理一批，还有约 " + run.result.remaining_pages
                + " 页；可点“继续补查”", "warn");
              $("rescan-continue-btn").classList.remove("hidden");
            } else if (run.status === "succeeded") {
              msg($("rescan-msg"), "补查完成：新进待审 " + run.result.imported_count
                + "，日期排除 " + run.result.date_excluded_count
                + "，时间待确认 " + run.result.date_unresolved_count, "ok");
              state.activeRescan = null;
            } else if (["failed", "cancelled", "interrupted"].indexOf(run.status) >= 0) {
              msg($("rescan-msg"), (run.error && run.error.message) || run.status, "error");
              if (run.status === "failed") {
                $("rescan-continue-btn").classList.remove("hidden");
              }
            }
            if (["succeeded", "cancelled", "interrupted"].indexOf(run.status) >= 0) {
              $("rescan-cancel-btn").classList.add("hidden");
            }
          });
        });
    }).catch(function (e) { msg($("rescan-msg"), e.message, "error"); });
  }

  $("rescan-start-btn").addEventListener("click", function () { startRescan(false); });
  $("rescan-continue-btn").addEventListener("click", function () { startRescan(true); });
  $("rescan-cancel-btn").addEventListener("click", function () {
    api("/api/operator/runs/" + state.currentRunId, json("POST", {}))
      .then(function () { msg($("rescan-msg"), "已请求取消补查", "warn"); })
      .catch(function (e) { msg($("rescan-msg"), e.message, "error"); });
  });

  /* ---- 设置 ---- */

  $("new-ws-validate-btn").addEventListener("click", function () {
    api("/api/operator/validate-source-path", json("POST", {
      path: $("new-ws-source").value,
    })).then(function (r) {
      state.newWorkspaceSourceId = r.source_id;
      msg($("workspace-msg"), "校验通过：" + r.file_name + "（sha256 "
        + String(r.sha256).slice(0, 12) + "…）；可点击新建", "ok");
    }).catch(function (e) { msg($("workspace-msg"), e.message, "error"); });
  });

  $("new-ws-create-btn").addEventListener("click", function () {
    if (!state.newWorkspaceSourceId) {
      msg($("workspace-msg"), "请先校验副本路径", "warn");
      return;
    }
    if (!window.confirm("将以该校验副本新建工作区并切换；旧工作区保留，重启后进入新工作区。继续吗？")) {
      return;
    }
    api("/api/operator/workspaces/new", json("POST", {
      source_id: state.newWorkspaceSourceId,
      name: $("new-ws-name").value || null,
      confirmed: true,
    })).then(function (r) {
      state.newWorkspaceSourceId = null;
      msg($("workspace-msg"), "已新建工作区 " + r.workspace_id.slice(0, 8)
        + "…；请退出并重新启动助手进入", "ok");
      loadSettings();
    }).catch(function (e) { msg($("workspace-msg"), e.message, "error"); });
  });

  $("manual-import-btn").addEventListener("click", function () {
    api("/api/operator/import", json("POST", { raw_text: $("manual-raw").value }))
      .then(function (r) { msg($("manual-msg"), "已导入 #" + r.id, "ok"); $("manual-raw").value = ""; })
      .catch(function (e) { msg($("manual-msg"), e.message, "error"); });
  });

  $("shutdown-btn").addEventListener("click", function () {
    api("/api/operator/shutdown", json("POST", { confirmed: true }))
      .then(function () { $("status-diagnostic").textContent = "已退出，可关闭本页面"; })
      .catch(function () {});
  });

  window.addEventListener("beforeunload", function (e) {
    if (hasUnsaved()) {
      e.preventDefault();
      e.returnValue = "有未保存修改，确定离开？";
    }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
