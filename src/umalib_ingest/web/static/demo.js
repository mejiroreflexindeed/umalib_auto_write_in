/* TASK-013 引导式 Demo UI（M08/M09）。

只在 /api/demo/status 可用时启用；legacy 模式完全不受影响。所有不可信字符串
只用 textContent/安全 DOM 写入，不喂 innerHTML。非 loopback 请求不会产生。
*/
"use strict";

(function () {
  var enabled = false;
  var status = null;
  var activeSection = "collect";
  var currentItemId = null;

  function $(id) {
    return document.getElementById(id);
  }

  function api(path, options) {
    return fetch(path, options || {}).then(function (res) {
      return res.json().then(function (body) {
        if (body && body.ok === true) {
          return body.data;
        }
        throw new Error(
          body && body.error ? "[" + body.error.code + "] " + body.error.message : "请求失败"
        );
      });
    });
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = text;
    }
    return node;
  }

  function button(label, onClick, className) {
    var node = el("button", className || "", label);
    node.type = "button";
    node.addEventListener("click", onClick);
    return node;
  }

  function message(node, text, kind) {
    node.textContent = "";
    node.appendChild(el("span", kind || "", text));
  }

  function showSection(name) {
    activeSection = name;
    var sections = ["collect", "review", "deliver"];
    for (var i = 0; i < sections.length; i++) {
      var sec = $("demo-section-" + sections[i]);
      if (sec) {
        sec.classList.toggle("demo-hidden", sections[i] !== name);
      }
    }
    var buttons = document.querySelectorAll("#demo-nav button");
    for (var j = 0; j < buttons.length; j++) {
      buttons[j].classList.toggle("active", buttons[j].dataset.section === name);
    }
    if (name === "review") {
      loadReviewList();
    }
    if (name === "deliver") {
      loadDeliveries();
    }
  }

  /* ---------- 收集 ---------- */

  function buildCollect() {
    var section = $("demo-section-collect");
    section.textContent = "";

    var manual = el("div", "demo-card");
    manual.appendChild(el("h3", "", "粘贴楼层原文"));
    var textarea = el("textarea", "");
    textarea.rows = 5;
    textarea.style.width = "100%";
    manual.appendChild(textarea);
    var msg = el("div", "demo-muted");
    manual.appendChild(msg);
    manual.appendChild(button("导入 Inbox（待审）", function () {
      api("/api/demo/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ raw_text: textarea.value }),
      }).then(function (data) {
        message(msg, "已导入，记录 ID " + data.id + "（状态：" + data.status + "）", "demo-ok");
        textarea.value = "";
      }).catch(function (err) {
        message(msg, err.message, "demo-error");
      });
    }));

    var samples = el("div", "demo-card");
    samples.appendChild(el("h3", "", "内置演示样例"));
    var sampleMsg = el("div", "demo-muted");
    samples.appendChild(sampleMsg);
    api("/api/demo/samples").then(function (data) {
      for (var i = 0; i < data.samples.length; i++) {
        (function (sample) {
          var row = el("div", "");
          row.appendChild(el("b", "", sample.title + "："));
          row.appendChild(el("span", "demo-muted", " " + sample.description));
          row.appendChild(button("导入" + sample.title, function () {
            api("/api/demo/samples/import", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ sample_id: sample.sample_id }),
            }).then(function (imported) {
              message(sampleMsg, "已导入 " + sample.title + "（状态：" + imported.status + "）→ " + sample.next_step, "demo-ok");
            }).catch(function (err) {
              message(sampleMsg, err.message, "demo-error");
            });
          }));
          samples.appendChild(row);
        })(data.samples[i]);
      }
    }).catch(function (err) {
      message(sampleMsg, err.message, "demo-error");
    });
    section.appendChild(manual);
    section.appendChild(samples);

    if (status && status.simulated_scan_only) {
      var sim = el("div", "demo-card");
      sim.appendChild(el("h3", "", "离线模拟扫描"));
      sim.appendChild(el("p", "demo-warn", "离线模拟，不代表已连接 NGA"));
      var simMsg = el("div", "demo-muted");
      sim.appendChild(simMsg);
      sim.appendChild(button("场景一：讨论+投稿+缺字段", function () {
        runSimulated("sim-01-basics", simMsg);
      }));
      sim.appendChild(button("场景二：同楼更新", function () {
        runSimulated("sim-02-update", simMsg);
      }));
      section.appendChild(sim);
    }

    if (status && status.nga_enabled) {
      var nga = el("div", "demo-card");
      nga.appendChild(el("h3", "", "NGA 检查新增"));
      nga.appendChild(el("p", "demo-muted", "从现在开始：不补录历史，旧页日后编辑不保证被发现"));
      var ngaMsg = el("div", "demo-muted");
      nga.appendChild(ngaMsg);
      var previewBtn = button("读取目标帖并形成初始化建议（需人工确认）", function () {
        api("/api/demo/nga/baseline-preview", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirm_read: true }),
        }).then(function (data) {
          ngaPreview = data;
          message(ngaMsg, "预览完成：尾页 " + data.preview.last_page + "，楼层 " + data.preview.floor_pids.length + " 个；请人工确认后点击初始化", "demo-ok");
        }).catch(function (err) {
          message(ngaMsg, err.message, "demo-error");
        });
      });
      nga.appendChild(previewBtn);
      var confirmBtn = button("确认：从现在开始（不补录历史）", function () {
        if (!ngaPreview) {
          message(ngaMsg, "请先点击读取", "demo-error");
          return;
        }
        api("/api/demo/nga/baseline-confirm", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            preview_id: ngaPreview.preview.preview_id,
            observed_sha256: ngaPreview.preview.observed_bytes_sha256,
            confirm_skip_history: true,
          }),
        }).then(function (data) {
          message(ngaMsg, "基线已建立：last_page=" + data.last_page + "，楼层 " + data.floor_count + " 个", "demo-ok");
        }).catch(function (err) {
          message(ngaMsg, err.message, "demo-error");
        });
      });
      nga.appendChild(confirmBtn);
      section.appendChild(nga);
    }
  }

  var ngaPreview = null;

  function runSimulated(scenarioId, msgNode) {
    api("/api/demo/simulated-scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario_id: scenarioId }),
    }).then(function (data) {
      var scan = data.scan;
      message(msgNode, "模拟扫描完成：新增 " + scan.new_floor_count + "，讨论 " + scan.discussion_count + "，触发 " + scan.triggered_count + "，导入 " + scan.imported_count + "，待人工复查 " + scan.changed_after_import.length, "demo-ok");
    }).catch(function (err) {
      message(msgNode, err.message, "demo-error");
    });
  }

  /* ---------- 审核 ---------- */

  function loadReviewList() {
    var listBox = $("demo-review-list");
    listBox.textContent = "";
    api("/api/items").then(function (data) {
      if (!data.items || data.items.length === 0) {
        listBox.appendChild(el("p", "demo-muted", "暂无待审稿件，请先在「收集稿件」导入。"));
        return;
      }
      for (var i = 0; i < data.items.length; i++) {
        (function (item) {
          var row = button(
            "#" + item.id + " " + (item.title || "（无标题）") + " [" + item.status + "]",
            function () { openReview(item.id); }
          );
          row.style.display = "block";
          row.style.textAlign = "left";
          listBox.appendChild(row);
        })(data.items[i]);
      }
    }).catch(function (err) {
      listBox.appendChild(el("p", "demo-error", err.message));
    });
  }

  function openReview(itemId) {
    currentItemId = itemId;
    var detail = $("demo-review-detail");
    detail.textContent = "";
    api("/api/items/" + itemId).then(function (item) {
      var fields = item.review_fields || {};
      var formatState = { mode: "plain", formatted_body: null, plain_text: null };
      detail.appendChild(el("h3", "", "① 核对正文（" + (fields.title || "无标题") + "）"));
      var form = el("div", "");
      function fieldRow(labelText, key) {
        var row = el("div", "");
        row.appendChild(el("span", "demo-step", labelText + " "));
        var input = el("input", "");
        input.value = fields[key] || "";
        input.style.width = "70%";
        input.dataset.field = key;
        row.appendChild(input);
        form.appendChild(row);
      }
      fieldRow("标题", "title");
      fieldRow("作者", "author");
      fieldRow("译者", "translator");
      fieldRow("系列", "series");
      fieldRow("标签(逗号分隔)", "tags");
      var bodyRow = el("div", "");
      bodyRow.appendChild(el("span", "demo-step", "正文 "));
      var body = el("textarea", "");
      body.rows = 6;
      body.style.width = "100%";
      body.value = fields.body_text || "";
      body.dataset.field = "body_text";
      bodyRow.appendChild(body);
      form.appendChild(bodyRow);
      detail.appendChild(form);

      detail.appendChild(el("h3", "", "格式建议（可选）"));
      var formatMsg = el("div", "demo-muted");
      detail.appendChild(formatMsg);
      var previewBox = el("div", "demo-muted");
      previewBox.id = "demo-detail-preview";
      detail.appendChild(previewBox);
      var adoptFormatButton = button("采用此格式并保存正文", function () {
        if (!formatState.formatted_body || formatState.plain_text === null) {
          message(formatMsg, "请先生成格式建议", "demo-error");
          return;
        }
        api("/api/items/" + itemId + "/review", {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(collectReviewPayload(formatState.plain_text)),
        }).then(function () {
          formatState.mode = "nga_ubb";
          message(formatMsg, "格式正文已采用并保存；之后请重新核对后批准", "demo-ok");
        }).catch(function (err) { message(formatMsg, err.message, "demo-error"); });
      });
      adoptFormatButton.disabled = true;
      detail.appendChild(button("从原文恢复基本格式（预览）", function () {
        api("/api/items/" + itemId).then(function (current) {
          api("/api/demo/format-preview", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            // /api/items/{id} exposes the immutable source as top-level raw_text.
            // Do not read an internal original_draft shape that the HTTP API never returns.
            body: JSON.stringify({ raw_text: current.raw_text || "" }),
          }).then(function (data) {
            formatState = {
              mode: "plain",
              formatted_body: data.source_body || null,
              plain_text: data.plain_text === undefined ? null : data.plain_text,
            };
            adoptFormatButton.disabled = !formatState.formatted_body || formatState.plain_text === null;
            renderFormatPreview(data, previewBox, formatMsg);
          }).catch(function (err) { message(formatMsg, err.message, "demo-error"); });
        });
      }));
      detail.appendChild(adoptFormatButton);

      detail.appendChild(el("h3", "", "② 保存与批准"));
      var actionMsg = el("div", "demo-muted");
      detail.appendChild(actionMsg);
      function collectReviewPayload(bodyOverride) {
        return {
          title: form.querySelector("[data-field=title]").value,
          author: form.querySelector("[data-field=author]").value,
          translator: form.querySelector("[data-field=translator]").value || null,
          series: form.querySelector("[data-field=series]").value || null,
          tags: (form.querySelector("[data-field=tags]").value || "").split(/[,，]/).map(function (s) { return s.trim(); }).filter(function (s) { return s !== ""; }),
          sources: {
            nga: (fields.sources && fields.sources.nga) || [],
            original_series: (fields.sources && fields.sources.original_series) || [],
            original: (fields.sources && fields.sources.original) || [],
            translation_series: (fields.sources && fields.sources.translation_series) || [],
            translation: (fields.sources && fields.sources.translation) || [],
          },
          body_text: bodyOverride === undefined ? form.querySelector("[data-field=body_text]").value : bodyOverride,
        };
      }
      detail.appendChild(button("保存修改", function () {
        var payload = collectReviewPayload();
        api("/api/items/" + itemId + "/review", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        }).then(function () {
          message(actionMsg, "已保存", "demo-ok");
        }).catch(function (err) { message(actionMsg, err.message, "demo-error"); });
      }));
      detail.appendChild(button("批准（approved）", function () {
        api("/api/items/" + itemId + "/transition", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ to_status: "approved", reason: "演示批准" }),
        }).then(function () {
          message(actionMsg, "已批准，请继续 ③ 来源/标签/格式 → ④ 预览并生成", "demo-ok");
        }).catch(function (err) { message(actionMsg, err.message, "demo-error"); });
      }));

      detail.appendChild(el("h3", "", "③ 来源 / 标签 / 格式决定"));
      var choiceMsg = el("div", "demo-muted");
      detail.appendChild(choiceMsg);
      var uploadInput = el("input", "");
      uploadInput.placeholder = "上传时间 Unix 秒（如 1788062400）";
      uploadInput.dataset.field = "upload";
      detail.appendChild(uploadInput);
      var noteInput = el("input", "");
      noteInput.placeholder = "备注（可空）";
      noteInput.dataset.field = "note";
      detail.appendChild(noteInput);
      var sourcesInput = el("input", "");
      sourcesInput.placeholder = "来源 URL（空格分隔，按人工顺序）";
      sourcesInput.dataset.field = "sources";
      sourcesInput.style.width = "100%";
      detail.appendChild(sourcesInput);
      var tagChoiceBox = el("div", "demo-tag-choices");
      tagChoiceBox.appendChild(el("p", "demo-muted", "标签/系列决定（唯一匹配可留空自动复用；其他项目必须明确选择）"));
      var tagCatalogMsg = el("div", "demo-muted", "正在读取当前候选库标签目录…");
      tagChoiceBox.appendChild(tagCatalogMsg);
      detail.appendChild(tagChoiceBox);
      function loadTagChoices() {
        api("/api/demo/catalog").then(function (catalogData) {
          tagChoiceBox.textContent = "";
          tagChoiceBox.appendChild(el("p", "demo-muted", "标签/系列决定（唯一匹配可留空自动复用；其他项目必须明确选择）"));
          var requested = (form.querySelector("[data-field=tags]").value || "").split(/[,，]/).map(function (s) { return s.trim(); }).filter(Boolean).map(function (value) { return { kind: "tag", value: value }; });
          var seriesValue = form.querySelector("[data-field=series]").value.trim();
          if (seriesValue) { requested.push({ kind: "series", value: seriesValue }); }
          for (var i = 0; i < requested.length; i++) {
            (function (request) {
              var matches = catalogData.tags.filter(function (tag) { return tag.name === request.value; });
              var row = el("div", "demo-tag-choice-row");
              row.appendChild(el("span", "", request.kind + "「" + request.value + "」："));
              var select = el("select", "");
              select.dataset.tagKind = request.kind;
              select.dataset.tagValue = request.value;
              var blank = el("option", "", matches.length === 1 ? "唯一匹配，自动复用" : "请选择");
              blank.value = "";
              select.appendChild(blank);
              for (var j = 0; j < matches.length; j++) {
                var existing = el("option", "", "使用 ID " + matches[j].id + "（type=" + matches[j].type + "，使用 " + (matches[j].usage_count || 0) + "）");
                existing.value = "existing:" + matches[j].id;
                select.appendChild(existing);
              }
              if (matches.length === 0) {
                for (var type = 0; type <= 4; type++) {
                  var created = el("option", "", "新建标签 type=" + type);
                  created.value = "new:" + type;
                  select.appendChild(created);
                }
              }
              row.appendChild(select);
              tagChoiceBox.appendChild(row);
            })(requested[i]);
          }
          if (!requested.length) { tagChoiceBox.appendChild(el("p", "demo-muted", "没有标签/系列请求")); }
        }).catch(function (err) { tagCatalogMsg.textContent = "标签目录读取失败：" + err.message + "；请刷新后重试"; });
      }
      loadTagChoices();
      var lossLabel = el("label", "", " 我确认以纯文本写入并接受格式损失 ");
      var lossCheck = el("input", "");
      lossCheck.type = "checkbox";
      lossLabel.appendChild(lossCheck);
      detail.appendChild(lossLabel);
      detail.appendChild(button("保存决定", function () {
        var sources = (sourcesInput.value || "").split(/[\s]+/).filter(function (s) { return s !== ""; });
        api("/api/demo/source-preview", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ urls: sources }),
        }).then(function (preview) {
          var normalized = preview.results.map(function (r) {
            if (r.blockers && r.blockers.length) { throw new Error("来源无法规范化: " + r.original); }
            return r.normalized;
          });
          api("/api/demo/items/" + itemId + "/choices", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              note: noteInput.value || null,
              upload_time_seconds: parseInt(uploadInput.value || "0", 10),
              ordered_sources: normalized,
              tag_choices: Array.prototype.map.call(tagChoiceBox.querySelectorAll("select[data-tag-kind]"), function (select) {
                if (!select.value) { return null; }
                var choice = { requested_kind: select.dataset.tagKind, requested_value: select.dataset.tagValue, existing_tag_id: null, new_tag_type: null };
                if (select.value.indexOf("existing:") === 0) { choice.existing_tag_id = parseInt(select.value.slice(9), 10); }
                else { choice.new_tag_type = parseInt(select.value.slice(4), 10); }
                return choice;
              }).filter(function (choice) { return choice !== null; }),
              content_mode: formatState.mode,
              formatted_body: formatState.mode === "nga_ubb" ? formatState.formatted_body : null,
              plain_loss_confirmed: lossCheck.checked,
            }),
          }).then(function () {
            message(choiceMsg, "决定已保存；请点击下方 ④ 预览", "demo-ok");
          }).catch(function (err) { message(choiceMsg, err.message, "demo-error"); });
        }).catch(function (err) { message(choiceMsg, err.message, "demo-error"); });
      }));

      detail.appendChild(el("h3", "", "④ 预览并生成候选"));
      detail.appendChild(el("p", "demo-warn", "这是新候选副本，不会替换正式库"));
      var writerMsg = el("div", "demo-muted");
      detail.appendChild(writerMsg);
      var closeCheck = el("input", "");
      closeCheck.type = "checkbox";
      var closeLabel = el("label", "", " 我已确认大书库已关闭（app_closed_confirmed）");
      closeLabel.appendChild(closeCheck);
      detail.appendChild(closeLabel);
      var genBtn = button("生成候选", function () {
        api("/api/demo/status").then(function (st) {
          var operationId = newOperationId();
          return api("/api/demo/items/" + itemId + "/choices").then(function (choicesData) {
            var choices = choicesData.choices || {};
            var writeDto = {
              candidate_file_name: "candidate-" + operationId + ".db",
              choices: {
                upload_time_seconds: choices.upload_time_seconds || 0,
                note: choices.note || null,
                ordered_sources: choices.ordered_sources || [],
                tag_choices: choices.tag_choices || [],
                content_mode: choices.content_mode || "plain",
                formatted_body: choices.formatted_body || null,
                plain_loss_confirmed: choices.plain_loss_confirmed === true,
              },
              expected_plan_sha256: "",
              expected_preview_sha256: "",
              expected_source_database_sha256: "",
              app_closed_confirmed: closeCheck.checked,
              operation_id: operationId,
              workspace_revision: st.revision,
            };
            return api("/api/items/" + itemId + "/writer/preview", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ candidate_file_name: writeDto.candidate_file_name, choices: writeDto.choices }),
            }).then(function (preview) {
              writeDto.expected_plan_sha256 = preview.plan.plan_sha256;
              writeDto.expected_preview_sha256 = preview.preview.preview_sha256;
              writeDto.expected_source_database_sha256 = preview.plan.input.target_database_sha256;
              return api("/api/items/" + itemId + "/writer/write", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(writeDto),
              }).then(function (result) {
                var delivery = result.delivery || {};
                var resultCounts = (result.result && result.result.counts_after) || {};
                var countAfter = delivery.count_after || resultCounts.article || 0;
                message(writerMsg, "候选已生成并验证；正式库未被替换。最新候选包含本工作区已录入 " + countAfter + " 篇", "demo-ok");
              });
            });
          });
        }).catch(function (err) { message(writerMsg, err.message, "demo-error"); });
      });
      detail.appendChild(genBtn);
    }).catch(function (err) {
      detail.appendChild(el("p", "demo-error", err.message));
    });
  }

  function newOperationId() {
    function part() {
      return Math.random().toString(16).slice(2, 6);
    }
    return part() + part() + "-" + part() + "-4" + part().slice(1) + "-8" + part().slice(1) + "-" + part() + part() + part();
  }

  function renderFormatPreview(data, previewBox, msgNode) {
    previewBox.textContent = "";
    var diag = data.diagnostics || [];
    var blockers = diag.filter(function (d) { return d.severity === "blocker"; });
    if (blockers.length) {
      previewBox.appendChild(el("p", "demo-error", "存在格式问题：" + blockers.map(function (d) { return d.code; }).join(", ")));
      return;
    }
    previewBox.appendChild(el("p", "", "纯文本：" + data.plain_text));
    var show = el("div", "");
    // 安全预览：用 document 的 runs 创建 DOM，不用 innerHTML
    if (data.html) {
      var preview = buildPreviewNodes(data.html);
      if (preview) {
        show.appendChild(preview);
        previewBox.appendChild(el("p", "demo-muted", "格式预览（安全渲染）："));
        previewBox.appendChild(show);
      }
    }
    message(msgNode, "建议已生成；如需采用，请手动核对并保存正文", "demo-ok");
  }

  function buildPreviewNodes(html) {
    var wrapper = el("div", "");
    var parser = new DOMParser();
    var doc = parser.parseFromString(html, "text/html");
    var nodes = doc.body.childNodes;
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      if (node.nodeType !== 1) {
        continue;
      }
      if (node.tagName === "P") {
        var p = el("p", "");
        appendRuns(p, node);
        wrapper.appendChild(p);
      }
    }
    return wrapper;
  }

  function appendRuns(target, htmlNode) {
    for (var i = 0; i < htmlNode.childNodes.length; i++) {
      var child = htmlNode.childNodes[i];
      if (child.nodeType === 3) {
        target.appendChild(document.createTextNode(child.textContent));
        continue;
      }
      if (child.nodeType !== 1) {
        continue;
      }
      var tag = child.tagName;
      var style = null;
      if (tag === "BR") {
        target.appendChild(el("br", ""));
        continue;
      }
      if (tag === "STRONG") { style = "bold"; }
      else if (tag === "EM") { style = "italic"; }
      else if (tag === "U") { style = "underline"; }
      else if (tag === "S") { style = "strike"; }
      else if (tag === "SPAN" && child.getAttribute("style")) {
        var span = el("span", "");
        span.style.color = child.getAttribute("style").replace(/^color:\s*/, "").replace(/;$/, "");
        appendRuns(span, child);
        target.appendChild(span);
        continue;
      }
      var inner = el("span", "");
      if (style === "bold") { inner.style.fontWeight = "bold"; }
      if (style === "italic") { inner.style.fontStyle = "italic"; }
      if (style === "underline") { inner.style.textDecoration = "underline"; }
      if (style === "strike") { inner.style.textDecoration = "line-through"; }
      appendRuns(inner, child);
      target.appendChild(inner);
    }
  }

  /* ---------- 交付与恢复 ---------- */

  function loadDeliveries() {
    var box = $("demo-deliver-list");
    box.textContent = "";
    var statusBox = $("demo-deliver-status");
    statusBox.textContent = "";
    api("/api/demo/status").then(function (st) {
      status = st;
      statusBox.appendChild(el("p", "", "工作区：" + st.name + "（" + st.mode + "） revision=" + st.revision +
        (st.head ? "；最新候选 " + st.head.candidate_file_name : "；尚无候选")));
      if (st.recovery_findings && st.recovery_findings.length) {
        var warn = el("div", "demo-warn");
        warn.appendChild(el("p", "", "存在需要恢复/核对的未完成操作："));
        for (var i = 0; i < st.recovery_findings.length; i++) {
          (function (finding) {
            var row = el("div", "");
            row.appendChild(el("span", "", finding.operation_id + " → " + finding.state + "（" + finding.reason + "）"));
            for (var k = 0; k < (finding.allowed_actions || []).length; k++) {
              (function (action) {
                row.appendChild(button(action === "complete" ? "完成恢复" : "放弃未验证尝试", function () {
                  api("/api/demo/operations/" + finding.operation_id, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ action: action, confirmed: true }),
                  }).then(function () {
                    loadDeliveries();
                  }).catch(function (err) {
                    row.appendChild(el("span", "demo-error", " " + err.message));
                  });
                }));
              })(finding.allowed_actions[k]);
            }
            warn.appendChild(row);
          })(st.recovery_findings[i]);
        }
        statusBox.appendChild(warn);
      }
    }).then(function () {
      return api("/api/demo/deliveries");
    }).then(function (data) {
      if (!data.deliveries.length) {
        box.appendChild(el("p", "demo-muted", "暂无交付记录"));
      }
      var table = el("table", "demo-table");
      var head = el("tr", "");
      ["最新", "篇数", "候选文件", "时间"].forEach(function (t) {
        head.appendChild(el("th", "", t));
      });
      table.appendChild(head);
      for (var i = 0; i < data.deliveries.length; i++) {
        (function (delivery) {
          var row = el("tr", delivery.latest ? "demo-candidate-row latest" : "demo-candidate-row");
          row.appendChild(el("td", "", delivery.latest ? "★ 最新" : ""));
          row.appendChild(el("td", "", "已录入 " + delivery.count_after + " 篇"));
          row.appendChild(el("td", "", delivery.candidate_file_name));
          row.appendChild(el("td", "", delivery.created_at.slice(0, 19)));
          table.appendChild(row);
        })(data.deliveries[i]);
      }
      box.appendChild(table);
    }).catch(function (err) {
      box.appendChild(el("p", "demo-error", err.message));
    });

    var cpBox = $("demo-checkpoint-box");
    cpBox.textContent = "";
    cpBox.appendChild(el("h3", "", "工作区检查点"));
    var cpMsg = el("div", "demo-muted");
    cpBox.appendChild(cpMsg);
    cpBox.appendChild(button("创建检查点", function () {
      api("/api/demo/checkpoints", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirmed: true }),
      }).then(function () {
        message(cpMsg, "检查点已创建", "demo-ok");
        loadCheckpointList(cpBox);
      }).catch(function (err) { message(cpMsg, err.message, "demo-error"); });
    }));
    cpBox.appendChild(el("div", "demo-muted", "恢复会创建一个新工作区并返回新 ID，需重启后进入"));
    loadCheckpointList(cpBox);
  }

  function loadCheckpointList(cpBox) {
    api("/api/demo/checkpoints").then(function (data) {
      for (var i = 0; i < data.checkpoints.length; i++) {
        (function (cp) {
          var row = el("div", "");
          row.appendChild(el("span", "", cp.checkpoint_id + " revision=" + cp.revision + " " + (cp.created_at || "").slice(0, 19)));
          row.appendChild(button("恢复为新工作区", function () {
            api("/api/demo/checkpoints/" + cp.checkpoint_id + "/restore", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ confirmed: true }),
            }).then(function (restored) {
              row.appendChild(el("span", "demo-ok", " 新工作区：" + restored.new_workspace_id + "（重启后进入）"));
            }).catch(function (err) {
              row.appendChild(el("span", "demo-error", " " + err.message));
            });
          }));
          cpBox.appendChild(row);
        })(data.checkpoints[i]);
      }
    }).catch(function () {});
  }

  /* ---------- 启动 ---------- */

  function boot() {
    api("/api/demo/status").then(function (st) {
      enabled = true;
      status = st;
      var host = el("div", "");
      host.id = "demo-app";
      var titleBar = el("div", "");
      titleBar.id = "demo-title-bar";
      titleBar.appendChild(el("span", "", "大书库录入助手"));
      titleBar.appendChild(el("span", "demo-ws", st.name + "（" + st.mode + "）· 本机运行"));
      host.appendChild(titleBar);
      var nav = el("div", "");
      nav.id = "demo-nav";
      var collectBtn = button("收集稿件", function () { showSection("collect"); });
      collectBtn.dataset.section = "collect";
      var reviewBtn = button("审核录入", function () { showSection("review"); });
      reviewBtn.dataset.section = "review";
      var deliverBtn = button("交付与恢复", function () { showSection("deliver"); });
      deliverBtn.dataset.section = "deliver";
      nav.appendChild(collectBtn);
      nav.appendChild(reviewBtn);
      nav.appendChild(deliverBtn);
      host.appendChild(nav);

      var collect = el("div", "demo-section demo-grid");
      collect.id = "demo-section-collect";
      var review = el("div", "demo-section");
      review.id = "demo-section-review";
      var reviewGrid = el("div", "demo-grid");
      var listCol = el("div", "demo-card");
      listCol.appendChild(el("h3", "", "Inbox 列表"));
      var listBox = el("div", "");
      listBox.id = "demo-review-list";
      listCol.appendChild(listBox);
      var detailCol = el("div", "demo-card");
      detailCol.style.width = "560px";
      detailCol.id = "demo-review-detail";
      detailCol.appendChild(el("p", "demo-muted", "点击左侧记录开始审核"));
      reviewGrid.appendChild(listCol);
      reviewGrid.appendChild(detailCol);
      review.appendChild(reviewGrid);

      var deliver = el("div", "demo-section");
      deliver.id = "demo-section-deliver";
      var statusBox = el("div", "");
      statusBox.id = "demo-deliver-status";
      var deliverBox = el("div", "");
      deliverBox.id = "demo-deliver-list";
      var cpBox = el("div", "");
      cpBox.id = "demo-checkpoint-box";
      deliver.appendChild(statusBox);
      deliver.appendChild(deliverBox);
      deliver.appendChild(cpBox);

      host.appendChild(collect);
      host.appendChild(review);
      host.appendChild(deliver);
      var footer = el("div", "demo-muted");
      footer.id = "demo-footer";
      footer.textContent = "当前状态 / 服务连接正常 / 诊断详情：本页面全部操作只写本工作区候选副本，不会替换正式库";
      host.appendChild(footer);

      var body = document.body;
      body.insertBefore(host, body.firstChild);
      buildCollect();
      showSection("collect");
    }).catch(function () {
      // 非 demo 模式：不改变现有页面
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
