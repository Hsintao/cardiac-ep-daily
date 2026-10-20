/* 心脏电生理日报 —— 前端渲染
 *
 * 架构对齐参考项目 daily-arXiv-ai-enhanced：
 *   页面只是壳，选日期后 fetch 对应 JSONL 就地渲染，不跳转、不刷新。
 *
 * 数据与页面同源（都发布在 data 分支根目录），用相对路径 fetch 即可。
 * 所有论文文本必须经 escapeHtml 转义后再插入 DOM，详见 renderPaper。
 */
(function () {
    "use strict";

    var cfg = JSON.parse(document.getElementById("site-config").textContent);
    var state = { day: null, papers: [], topic: "all", journal: "all", query: "" };
    var cache = {};    // day -> papers[]，切换回来时不重复请求

    var el = {
        container: document.getElementById("paperContainer"),
        topicFilters: document.getElementById("topicFilters"),
        journalFilters: document.getElementById("journalFilters"),
        search: document.getElementById("textSearch"),
        chipTotal: document.getElementById("chipTotal"),
        chipAI: document.getElementById("chipAI"),
        chipNoAbs: document.getElementById("chipNoAbs"),
        chipNoAbsWrap: document.getElementById("chipNoAbsWrap"),
        dateToggleText: document.getElementById("dateToggleText"),
        dateToggle: document.getElementById("dateToggle"),
        datePanel: document.getElementById("datePanel"),
    };

    var TOPIC_ORDER = ["房颤与消融", "室性心律失常", "器械治疗", "影像与标测", "遗传与基础", "其他"];

    /* ---------------------------------------------------------------- 工具 */

    function escapeHtml(value) {
        if (value === null || value === undefined) { return ""; }
        return String(value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    /* 链接同样要过一遍：只放行 http(s)，避免 javascript: 之类伪协议进入 href */
    function safeUrl(url) {
        if (!url) { return ""; }
        var text = String(url).trim();
        if (/^https?:\/\//i.test(text)) { return escapeHtml(text); }
        return "";
    }

    function dataUrlFor(day) {
        return "data/" + encodeURIComponent(day) + '_AI_enhanced_' + cfg.language + ".jsonl";
    }

    /* ---------------------------------------------------------- 数据加载 */

    function loadDay(day) {
        if (cache[day]) { return Promise.resolve(cache[day]); }
        return fetch(dataUrlFor(day), { cache: "no-cache" })
            .then(function (resp) {
                if (!resp.ok) { throw new Error("HTTP " + resp.status); }
                return resp.text();
            })
            .then(function (text) {
                var papers = [];
                text.split("\n").forEach(function (line) {
                    line = line.trim();
                    if (!line) { return; }
                    try {
                        var item = JSON.parse(line);
                        if (item && item.title) { papers.push(item); }
                    } catch (e) { /* 坏行跳过，不影响其它论文 */ }
                });
                papers.sort(function (a, b) {
                    var ia = TOPIC_ORDER.indexOf(a.topic);
                    var ib = TOPIC_ORDER.indexOf(b.topic);
                    if (ia !== ib) { return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib); }
                    return String(a.title).localeCompare(String(b.title));
                });
                cache[day] = papers;
                return papers;
            });
    }

    /* ------------------------------------------------------------ 渲染 */

    function renderChipCounts(papers) {
        var aiCount = 0, noAbsCount = 0;
        papers.forEach(function (p) {
            if (p.AI && p.AI.tldr) { aiCount += 1; }
            if (p.no_abstract) { noAbsCount += 1; }
        });
        el.chipTotal.textContent = papers.length;
        el.chipAI.textContent = aiCount;
        if (noAbsCount > 0) {
            el.chipNoAbs.textContent = noAbsCount;
            el.chipNoAbsWrap.hidden = false;
        } else {
            el.chipNoAbsWrap.hidden = true;
        }
    }

    function renderTopicFilters(papers) {
        var counts = {};
        papers.forEach(function (p) {
            var t = TOPIC_ORDER.indexOf(p.topic) >= 0 ? p.topic : "其他";
            counts[t] = (counts[t] || 0) + 1;
        });
        var html = '<button type="button" class="filter-btn active" data-filter-topic="all">全部 <span class="count">' +
            papers.length + "</span></button>";
        TOPIC_ORDER.forEach(function (topic) {
            if (!counts[topic]) { return; }
            html += '<button type="button" class="filter-btn" data-filter-topic="' + escapeHtml(topic) +
                '">' + escapeHtml(topic) + ' <span class="count">' + counts[topic] + "</span></button>";
        });
        el.topicFilters.innerHTML = html;
    }

    function renderJournalFilters(papers) {
        var counts = {};
        papers.forEach(function (p) {
            var j = p.journal || "未知期刊";
            counts[j] = (counts[j] || 0) + 1;
        });
        var names = Object.keys(counts).sort(function (a, b) {
            return counts[b] - counts[a] || a.localeCompare(b);
        });
        var html = '<button type="button" class="filter-btn active" data-filter-journal="all">全部</button>';
        names.forEach(function (name, index) {
            var extra = index >= 8 ? " extra-journal" : "";
            html += '<button type="button" class="filter-btn' + extra + '" data-filter-journal="' +
                escapeHtml(name) + '">' + escapeHtml(name) + ' <span class="count">' +
                counts[name] + "</span></button>";
        });
        if (names.length > 8) {
            html += '<button type="button" class="filter-btn more-btn" id="moreJournals">展开全部 ' +
                names.length + " 个期刊 ▾</button>";
        }
        el.journalFilters.innerHTML = html;
    }

    function renderAI(ai) {
        if (!ai || !ai.tldr) { return ""; }
        var sections = [
            ["tldr", "一句话结论"],
            ["motivation", "研究动机"],
            ["method", "方法"],
            ["result", "结果"],
            ["conclusion", "结论与意义"],
        ];
        var html = '<div class="ai-panel"><div class="ai-panel-head">● AI 中文解读</div>';
        sections.forEach(function (pair) {
            var key = pair[0], label = pair[1], value = ai[key];
            if (!value) { return; }
            var cls = key === "tldr" ? "ai-tldr" : "ai-section";
            html += '<div class="' + cls + '">' +
                '<span class="ai-label ' + key + '">' + escapeHtml(label) + "</span>" +
                "<p>" + escapeHtml(value) + "</p></div>";
        });
        return html + "</div>";
    }

    function renderPaper(paper, index) {
        var authors = paper.authors || [];
        var authorText = authors.slice(0, 3).join("、");
        if (authors.length > 3) {
            authorText += '<span class="authors-more"> 等 ' + authors.length + " 位作者</span>";
        }

        var meta = [];
        if (paper.journal) { meta.push('<span class="paper-journal">' + escapeHtml(paper.journal) + "</span>"); }
        if (paper.date) { meta.push('<span class="paper-date">' + escapeHtml(paper.date) + "</span>"); }
        if (paper.pmid) { meta.push('<span class="paper-date">PMID ' + escapeHtml(paper.pmid) + "</span>"); }

        var tags = '<span class="tag tag-topic" data-topic="' + escapeHtml(paper.topic || "其他") + '">' +
            escapeHtml(paper.topic || "其他") + "</span>";
        (paper.pub_types || []).slice(0, 3).forEach(function (t) {
            tags += '<span class="tag tag-type">' + escapeHtml(t) + "</span>";
        });
        if (paper.is_open_access) { tags += '<span class="tag tag-oa">开放获取</span>'; }

        var absUrl = safeUrl(paper.url || paper.abs);
        var titleHtml = absUrl
            ? '<a href="' + absUrl + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(paper.title) + "</a>"
            : escapeHtml(paper.title);

        var abstractHtml;
        if (paper.no_abstract || !(paper.summary || "").trim()) {
            abstractHtml = '<p class="abstract-none">暂无摘要（该文献可能为社论、评论或通信类文章）</p>';
        } else {
            abstractHtml = '<details class="abstract-block"><summary>英文摘要</summary><p>' +
                escapeHtml(paper.summary) + "</p></details>";
        }

        var links = "";
        if (absUrl) { links += '<a class="btn-link pubmed" href="' + absUrl + '" target="_blank" rel="noopener noreferrer">原文</a>'; }
        if (paper.doi) {
            var doiUrl = safeUrl("https://doi.org/" + paper.doi);
            if (doiUrl) { links += '<a class="btn-link doi" href="' + doiUrl + '" target="_blank" rel="noopener noreferrer">DOI</a>'; }
        }
        if (paper.pdf) {
            var pdfUrl = safeUrl(paper.pdf);
            if (pdfUrl) { links += '<a class="btn-link pdf" href="' + pdfUrl + '" target="_blank" rel="noopener noreferrer">PDF</a>'; }
        }

        var searchBlob = [paper.title, authorText, paper.summary, paper.journal]
            .join(" ").toLowerCase();

        return '<article class="paper-card"' +
            ' data-topic="' + escapeHtml(paper.topic || "其他") + '"' +
            ' data-journal="' + escapeHtml(paper.journal || "未知期刊") + '"' +
            ' data-search="' + escapeHtml(searchBlob) + '">' +
            '<div class="paper-top"><div class="paper-index">' + index + '</div>' +
            '<div class="paper-main">' +
            '<h3 class="paper-title">' + titleHtml + "</h3>" +
            '<div class="paper-meta">' + meta.join('<span class="dot">·</span>') + "</div>" +
            '<div class="tag-row">' + tags + "</div>" +
            (authorText ? '<p class="paper-authors">' + authorText + "</p>" : "") +
            abstractHtml +
            renderAI(paper.AI) +
            (links ? '<div class="paper-links">' + links + "</div>" : "") +
            "</div></div></article>";
    }

    function renderPapers() {
        var papers = state.papers;
        if (!papers.length) {
            el.container.innerHTML =
                '<div class="empty-state"><div class="icon">🗂️</div>' +
                "<h2>该日期没有论文</h2><p>" + escapeHtml(state.day) +
                " 这一天没有匹配到论文。周末与节假日尤其常见，可换一个日期查看。</p></div>";
            return;
        }
        var groups = {};
        papers.forEach(function (p) {
            var t = TOPIC_ORDER.indexOf(p.topic) >= 0 ? p.topic : "其他";
            (groups[t] = groups[t] || []).push(p);
        });
        var html = "";
        var index = 0;
        TOPIC_ORDER.forEach(function (topic) {
            var list = groups[topic];
            if (!list || !list.length) { return; }
            html += '<section class="topic-group" data-group-topic="' + escapeHtml(topic) + '">' +
                '<h2 class="topic-heading"><span class="bar"></span><span>' + escapeHtml(topic) +
                '</span><span class="topic-count">' + list.length + " 篇</span></h2>" +
                '<div class="paper-list">';
            list.forEach(function (p) {
                index += 1;
                html += renderPaper(p, index);
            });
            html += "</div></section>";
        });
        el.container.innerHTML = html;
        applyFilters();
        bindJournalToggle();
    }

    /* ------------------------------------------------------------ 筛选 */

    function applyFilters() {
        var q = state.query.trim().toLowerCase();
        var cards = el.container.querySelectorAll(".paper-card");
        var visible = 0;
        Array.prototype.forEach.call(cards, function (card) {
            var okTopic = state.topic === "all" || card.getAttribute("data-topic") === state.topic;
            var okJournal = state.journal === "all" || card.getAttribute("data-journal") === state.journal;
            var okQuery = !q || (card.getAttribute("data-search") || "").indexOf(q) >= 0;
            var show = okTopic && okJournal && okQuery;
            card.style.display = show ? "" : "none";
            if (show) { visible += 1; }
        });
        // 整组都被筛掉时隐藏组标题，避免出现空标题
        var sections = el.container.querySelectorAll(".topic-group");
        Array.prototype.forEach.call(sections, function (section) {
            var any = false;
            Array.prototype.forEach.call(section.querySelectorAll(".paper-card"), function (c) {
                if (c.style.display !== "none") { any = true; }
            });
            section.style.display = any ? "" : "none";
        });
        var emptyNote = document.getElementById("filterEmptyNote");
        if (visible === 0 && cards.length > 0) {
            if (!emptyNote) {
                emptyNote = document.createElement("div");
                emptyNote.id = "filterEmptyNote";
                emptyNote.className = "empty-state";
                emptyNote.innerHTML = '<div class="icon">🔍</div><h2>没有匹配的论文</h2>' +
                    "<p>试试清除筛选条件或换一个关键词。</p>";
                el.container.appendChild(emptyNote);
            }
            emptyNote.hidden = false;
        } else if (emptyNote) {
            emptyNote.hidden = true;
        }
    }

    function bindJournalToggle() {
        var btn = document.getElementById("moreJournals");
        if (!btn) { return; }
        var extras = el.journalFilters.querySelectorAll(".extra-journal");
        Array.prototype.forEach.call(extras, function (b) { b.style.display = "none"; });
        btn.addEventListener("click", function () {
            var open = btn.getAttribute("data-open") === "1";
            Array.prototype.forEach.call(extras, function (b) {
                b.style.display = open ? "none" : "";
            });
            btn.setAttribute("data-open", open ? "0" : "1");
            btn.textContent = open
                ? "展开全部 " + extras.length + " 个期刊 ▾"
                : "收起期刊列表 ▴";
        });
    }

    /* -------------------------------------------------------- 日期切换 */

    function setActiveDayCell(day) {
        el.datePanel.querySelectorAll(".date-cell").forEach(function (cell) {
            var on = cell.getAttribute("data-day") === day;
            cell.classList.toggle("current", on);
            cell.setAttribute("aria-selected", on ? "true" : "false");
        });
        el.dateToggleText.textContent = day;
    }

    function switchDay(day, options) {
        options = options || {};
        if (!day) { return; }
        state.day = day;
        state.topic = "all";
        state.journal = "all";
        state.query = "";
        el.search.value = "";
        setActiveDayCell(day);
        if (options.updateHash !== false && window.history && history.replaceState) {
            history.replaceState(null, "", "#" + day);
        }
        el.container.innerHTML =
            '<div class="loading-state"><div class="loading-spinner"></div><p>正在加载 ' +
            escapeHtml(day) + " 的论文…</p></div>";
        loadDay(day).then(function (papers) {
            state.papers = papers;
            renderChipCounts(papers);
            renderTopicFilters(papers);
            renderJournalFilters(papers);
            renderPapers();
        }).catch(function (err) {
            el.container.innerHTML =
                '<div class="empty-state"><div class="icon">⚠️</div><h2>加载失败</h2>' +
                "<p>" + escapeHtml(day) + " 的数据读取失败：" + escapeHtml(err.message) +
                '。请检查该日期的数据文件是否已发布。</p></div>';
        });
    }

    /* ------------------------------------------------------------ 绑定 */

    function initDatePicker() {
        el.dateToggle.addEventListener("click", function (event) {
            event.stopPropagation();
            var open = el.datePanel.hidden;
            el.datePanel.hidden = !open;
            el.dateToggle.setAttribute("aria-expanded", open ? "true" : "false");
            if (open) {
                var cur = el.datePanel.querySelector(".date-cell.current");
                if (cur && cur.scrollIntoView) { cur.scrollIntoView({ block: "nearest" }); }
            }
        });
        el.datePanel.addEventListener("click", function (event) {
            var cell = event.target.closest(".date-cell");
            if (cell) {
                event.stopPropagation();
                el.datePanel.hidden = true;
                el.dateToggle.setAttribute("aria-expanded", "false");
                switchDay(cell.getAttribute("data-day"));
                return;
            }
            event.stopPropagation();
        });
        document.addEventListener("click", function () {
            if (!el.datePanel.hidden) {
                el.datePanel.hidden = true;
                el.dateToggle.setAttribute("aria-expanded", "false");
            }
        });
        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && !el.datePanel.hidden) {
                el.datePanel.hidden = true;
                el.dateToggle.setAttribute("aria-expanded", "false");
                el.dateToggle.focus();
            }
        });
        var latest = document.getElementById("jumpLatest");
        if (latest) {
            latest.addEventListener("click", function (event) {
                event.stopPropagation();
                el.datePanel.hidden = true;
                switchDay(cfg.days[0]);
            });
        }
    }

    function initFilters() {
        el.topicFilters.addEventListener("click", function (event) {
            var btn = event.target.closest("[data-filter-topic]");
            if (!btn) { return; }
            el.topicFilters.querySelectorAll(".filter-btn").forEach(function (b) {
                b.classList.remove("active");
            });
            btn.classList.add("active");
            state.topic = btn.getAttribute("data-filter-topic");
            applyFilters();
        });
        el.journalFilters.addEventListener("click", function (event) {
            var btn = event.target.closest("[data-filter-journal]");
            if (!btn) { return; }
            el.journalFilters.querySelectorAll("[data-filter-journal]").forEach(function (b) {
                b.classList.remove("active");
            });
            btn.classList.add("active");
            state.journal = btn.getAttribute("data-filter-journal");
            applyFilters();
        });
        el.search.addEventListener("input", function () {
            state.query = el.search.value;
            applyFilters();
        });
    }

    function initTheme() {
        var btn = document.getElementById("themeToggle");
        if (!btn) { return; }
        var root = document.documentElement;
        var mql = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
        function current() {
            var manual = root.getAttribute("data-theme");
            if (manual === "dark" || manual === "light") { return manual; }
            return (mql && mql.matches) ? "dark" : "light";
        }
        function label() {
            var manual = root.getAttribute("data-theme");
            var suffix = manual ? "" : "（跟随系统）";
            btn.textContent = (current() === "dark" ? "☀️ 浅色" : "🌙 深色") + suffix;
        }
        btn.addEventListener("click", function () {
            var next = current() === "dark" ? "light" : "dark";
            root.setAttribute("data-theme", next);
            try { localStorage.setItem("ep-digest-theme", next); } catch (e) {}
            label();
        });
        label();
    }

    function init() {
        initDatePicker();
        initFilters();
        initTheme();
        // 支持用 #YYYY-MM-DD 直达某一天（可分享、刷新不丢）
        var fromHash = (location.hash || "").replace("#", "");
        var initial = cfg.days.indexOf(fromHash) >= 0 ? fromHash : cfg.defaultDate;
        switchDay(initial, { updateHash: false });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
