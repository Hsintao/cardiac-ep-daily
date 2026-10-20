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
    /* mode=single：只覆盖一天；mode=range：覆盖 startDay~endDay 之间所有有数据的天。
       startDay / endDay 始终表示「较早 / 较晚」端点；days 是区间内真实存在的数据日，
       按字符串升序比较即日期序，这里统一按日期倒序存放（最新在前）。
       区间模式下 day = days[0]（区间内最新的一天），供依赖 state.day 的逻辑兜底。 */
    var state = {
        mode: "single",
        /* rangeView 记录「当前正文展示的是不是区间聚合结果」。面板里的
           单日/区间开关只影响交互方式（state.mode），不改变已加载的视图；
           两者分开，切到单日但没点日期时顶栏与 chip 仍如实反映区间结果。 */
        rangeView: false,
        day: null,
        startDay: null,
        endDay: null,
        days: [],
        papers: [],
        topic: "all",
        journal: "all",
        query: ""
    };
    var cache = {};    // day -> papers[]，切换回来时不重复请求
    var pendingStart = null; // 区间模式里已点下的起始日，等待点结束日
    var loadToken = 0;       // 视图切换序号：慢请求回来时若已切走就丢弃，避免旧结果覆盖新视图
    var journalPanelGlobalBound = false; // 期刊浮层的全局事件只绑一次（重渲染会换元素）

    var el = {
        container: document.getElementById("paperContainer"),
        topicFilters: document.getElementById("topicFilters"),
        journalFilters: document.getElementById("journalFilters"),
        journalFilterLabel: document.getElementById("journalFilterLabel"),
        search: document.getElementById("textSearch"),
        chipTotal: document.getElementById("chipTotal"),
        chipAI: document.getElementById("chipAI"),
        chipNoAbs: document.getElementById("chipNoAbs"),
        chipNoAbsWrap: document.getElementById("chipNoAbsWrap"),
        chipDays: document.getElementById("chipDays"),
        chipDaysWrap: document.getElementById("chipDaysWrap"),
        dateToggleText: document.getElementById("dateToggleText"),
        dateToggleCount: document.getElementById("dateToggleCount"),
        dateToggle: document.getElementById("dateToggle"),
        datePanel: document.getElementById("datePanel"),
        modeSingle: document.getElementById("modeSingle"),
        modeRange: document.getElementById("modeRange"),
        dateRangeBar: document.getElementById("dateRangeBar"),
        dateRangeHint: document.getElementById("dateRangeHint"),
        clearRange: document.getElementById("clearRange"),
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

    /* ------------------------------------------------------ 日期区间（纯函数） */

    /* 日期一律用 "YYYY-MM-DD" 字符串表示：字典序即时间序，省掉 Date 解析，
       也避免时区把 00:00 的日期挪到前一天。以下函数不依赖 cfg / DOM，可被
       node 直接抽取出来单测（见 tests/test_render.py 的抽取方式）。 */

    function isDayString(value) {
        return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value);
    }

    /* 格式对了不代表日期存在（"2026-13-45" 也匹配上面的正则）。
       URL 是用户可以随手改的入口，所以这里再按历法校验一次：
       月份 1-12、日不超过当月天数（含闰年 2 月 29）。 */
    function isRealDay(day) {
        if (!isDayString(day)) { return false; }
        var year = parseInt(day.slice(0, 4), 10);
        var month = parseInt(day.slice(5, 7), 10);
        var date = parseInt(day.slice(8, 10), 10);
        if (month < 1 || month > 12 || date < 1) { return false; }
        var leap = (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
        var monthDays = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
        return date <= monthDays[month - 1];
    }

    /* URL hash → 视图描述：
         "#2026-10-01"                → {mode:"single", day}
         "#2026-10-01~2026-10-05"     → {mode:"range", start:较早, end:较晚}
         非法 / 空                    → null */
    function parseHash(hash) {
        if (hash === null || hash === undefined) { return null; }
        var text = String(hash);
        if (text.charAt(0) === "#") { text = text.slice(1); }
        if (!text) { return null; }
        var parts = text.split("~");
        if (parts.length === 2) {
            if (!isRealDay(parts[0]) || !isRealDay(parts[1])) { return null; }
            var first = parts[0], second = parts[1];
            return {
                mode: "range",
                start: first <= second ? first : second,
                end: first <= second ? second : first
            };
        }
        if (!isRealDay(text)) { return null; }
        return { mode: "single", day: text };
    }

    /* 区间 [start, end] 内真实有数据的天（含端点），按日期倒序返回。
       allDays 是构建时扫描出的日期列表，可能是升序也可能是倒序，这里不假设顺序。 */
    function daysBetween(start, end, allDays) {
        if (!isRealDay(start) || !isRealDay(end) || !allDays || !allDays.length) {
            return [];
        }
        var earlier = start <= end ? start : end;
        var later = start <= end ? end : start;
        var seen = {};
        var days = [];
        allDays.forEach(function (day) {
            if (!isDayString(day) || day < earlier || day > later) { return; }
            if (seen[day]) { return; }
            seen[day] = true;
            days.push(day);
        });
        days.sort();
        days.reverse();
        return days;
    }

    /* 区间按钮上的短文案：单日 → 完整 ISO；同年 → "MM-DD ~ MM-DD"；跨年 → 两端完整 ISO */
    function rangeLabel(start, end) {
        if (!isRealDay(start)) { return ""; }
        if (!end || end === start) { return start; }
        if (!isRealDay(end)) { return ""; }
        var earlier = start <= end ? start : end;
        var later = start <= end ? end : start;
        if (earlier.slice(0, 4) === later.slice(0, 4)) {
            return earlier.slice(5) + " ~ " + later.slice(5);
        }
        return earlier + " ~ " + later;
    }

    function dayShort(day) {
        if (!isDayString(day)) { return ""; }
        return day.slice(5);
    }

    /* 跨日聚合时判断「同一篇论文」的键，依次退让：
       同一 PMID 通常稳定出现，其次 DOI，再看内部 id，最后用标题 + 期刊指纹兜底。 */
    function dedupeKey(paper) {
        if (!paper) { return ""; }
        var pmid = paper.pmid;
        if (pmid !== null && pmid !== undefined && String(pmid).trim()) {
            return "pmid:" + String(pmid).trim().toLowerCase();
        }
        if (paper.doi && String(paper.doi).trim()) {
            return "doi:" + String(paper.doi).trim().toLowerCase();
        }
        if (paper.id && String(paper.id).trim()) {
            return "id:" + String(paper.id).trim();
        }
        var title = paper.title ? String(paper.title).trim().toLowerCase() : "";
        var journal = paper.journal ? String(paper.journal).trim().toLowerCase() : "";
        if (title || journal) {
            return "title:" + title + "|" + journal;
        }
        return "";
    }

    /* 保留首次出现的顺序：调用方按日期倒序喂入，因此留下的是最新收录日那条。 */
    function dedupePapers(papers) {
        var unique = [];
        if (!papers || !papers.length) { return unique; }
        var seen = {};
        papers.forEach(function (paper) {
            var key = dedupeKey(paper);
            if (key) {
                if (Object.prototype.hasOwnProperty.call(seen, key)) { return; }
                seen[key] = true;
            }
            unique.push(paper);
        });
        return unique;
    }

    /* 区间内论文的排序：收录日倒序 → 主题顺序 → 标题。返回副本，不改入参。 */
    function sortRangePapers(papers, topicOrder) {
        var list = (papers || []).slice();
        var order = topicOrder || [];
        function topicIndex(paper) {
            var index = order.indexOf(paper ? paper.topic : null);
            return index < 0 ? 99 : index;
        }
        list.sort(function (a, b) {
            var dayA = (a && a.__day) || "";
            var dayB = (b && b.__day) || "";
            if (dayA !== dayB) { return dayA < dayB ? 1 : -1; }
            var topicA = topicIndex(a);
            var topicB = topicIndex(b);
            if (topicA !== topicB) { return topicA - topicB; }
            return String((a && a.title) || "").localeCompare(String((b && b.title) || ""));
        });
        return list;
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

    /* 并发加载多天：每天单独 .catch，单日失败只记在该日上，不拖垮整个区间 */
    function loadDays(days) {
        return (days || []).map(function (day) {
            return loadDay(day).then(function (papers) {
                return { day: day, papers: papers, error: null };
            }).catch(function (err) {
                return { day: day, papers: [], error: err };
            });
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
        /* 覆盖天数只在正文是区间聚合时有意义；单日视图整个 chip 隐藏 */
        if (el.chipDaysWrap) {
            if (state.rangeView) {
                if (el.chipDays) { el.chipDays.textContent = state.days.length; }
                el.chipDaysWrap.hidden = false;
            } else {
                el.chipDaysWrap.hidden = true;
            }
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
        var VISIBLE = 8;   // 默认露出的期刊数，其余收进浮层

        function chip(name) {
            return '<button type="button" class="filter-btn" data-filter-journal="' +
                escapeHtml(name) + '">' + escapeHtml(name) + ' <span class="count">' +
                counts[name] + "</span></button>";
        }

        var html = '<button type="button" class="filter-btn active" data-filter-journal="all">全部</button>';
        names.slice(0, VISIBLE).forEach(function (name) {
            html += chip(name);
        });
        if (names.length > VISIBLE) {
            /* 期刊很多时收进浮层（绝对定位），而不是就地铺开：
               就地铺开会把吸顶的筛选栏越撑越高，最后整屏都是筛选栏，
               正文、甚至「收起」按钮都会被挤出屏幕。 */
            html += '<button type="button" class="filter-btn more-btn" id="moreJournals"' +
                ' aria-expanded="false" aria-controls="journalMorePanel">展开全部 ' +
                names.length + " 个期刊 ▾</button>";
            html += '<div class="journal-more-panel" id="journalMorePanel" hidden>' +
                '<div class="journal-more-grid">' +
                names.slice(VISIBLE).map(chip).join("") +
                "</div></div>";
        }
        el.journalFilters.innerHTML = html;
    }

    /* ---------------------------------------------------------- 摘要区 */

    /* 英文摘要与中文摘要（AI 翻译）都是 <details>，默认折叠。
       中文摘要只在数据里有 summary_zh 时渲染 —— 老数据或翻译失败的条目
       自然少一个折叠块，不会出现空壳。它排在英文摘要下面，位置固定，
       不与 AI 解读区抢视觉权重。 */
    function renderAbstracts(paper) {
        if (paper.no_abstract || !(paper.summary || "").trim()) {
            return '<p class="abstract-none">暂无摘要（该文献可能为社论、评论或通信类文章）</p>';
        }
        var html = '<details class="abstract-block"><summary>英文摘要</summary><p>' +
            escapeHtml(paper.summary) + "</p></details>";
        var summaryZh = (paper.summary_zh || "").trim();
        if (summaryZh) {
            html += '<details class="abstract-block abstract-zh">' +
                '<summary>中文摘要</summary>' +
                '<p class="abstract-zh-note">AI 翻译，仅供参考；准确内容以英文原文为准。</p>' +
                "<p>" + escapeHtml(summaryZh) + "</p></details>";
        }
        return html;
    }

    /* ---------------------------------------------------------- AI 解读 */

    function renderAI(ai) {
        if (!ai || !ai.tldr) { return ""; }
        var sections = [
            ["tldr", "一句话结论"],
            ["motivation", "研究动机"],
            ["method", "方法"],
            ["result", "结果"],
            ["conclusion", "结论与意义"],
        ];
        /* 一句话结论常驻展开（信息密度最高，值得直接看到）；
           其余四段（动机/方法/结果/结论）收进 <details>，默认折叠。
           两列布局下卡片本来就高，折叠后一屏能看更多篇。 */
        var html = '<div class="ai-panel"><div class="ai-panel-head">● AI 中文解读</div>';
        var detailCount = 0;
        var detailHtml = "";
        sections.forEach(function (pair) {
            var key = pair[0], label = pair[1], value = ai[key];
            if (!value) { return; }
            if (key === "tldr") {
                html += '<div class="ai-tldr">' +
                    '<span class="ai-label tldr">' + escapeHtml(label) + "</span>" +
                    "<p>" + escapeHtml(value) + "</p></div>";
                return;
            }
            detailCount += 1;
            detailHtml += '<div class="ai-section">' +
                '<span class="ai-label ' + key + '">' + escapeHtml(label) + "</span>" +
                "<p>" + escapeHtml(value) + "</p></div>";
        });
        if (detailHtml) {
            html += '<details class="ai-details">' +
                '<summary class="ai-details-summary">展开完整解读（动机 · 方法 · 结果 · 结论）</summary>' +
                '<div class="ai-details-body">' + detailHtml + "</div></details>";
        }
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
        /* 区间视图里同一篇论文只保留最新收录日那条，卡片上标出它被哪一天收录 */
        if (state.rangeView && paper.__day) {
            tags += '<span class="tag tag-day" title="收录日期">' +
                escapeHtml(dayShort(paper.__day)) + " 收录</span>";
        }

        var absUrl = safeUrl(paper.url || paper.abs);
        var titleHtml = absUrl
            ? '<a href="' + absUrl + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(paper.title) + "</a>"
            : escapeHtml(paper.title);

        var abstractHtml = renderAbstracts(paper);

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

        /* __day 也进检索串：区间视图里可以按「2026-10-05」这种收录日搜到卡片 */
        var searchBlob = [paper.title, authorText, paper.summary, paper.summary_zh, paper.journal, paper.__day]
            .join(" ").toLowerCase();

        /* 卡片结构对齐参考项目 daily-arXiv-ai-enhanced：
              paper-card
                ├── paper-card-header   ← 标题 / 期刊·日期 / 标签
                ├── paper-card-content  ← 作者 / 摘要 / AI 解读
                └── paper-card-footer   ← 链接按钮（对应参考项目的 card-footer）
           类名 paper-title / paper-meta / tag-row / paper-authors / abstract-block /
           ai-panel / paper-links 全部保留，供 assets/style.css 匹配。 */
        return '<article class="paper-card"' +
            ' data-topic="' + escapeHtml(paper.topic || "其他") + '"' +
            ' data-journal="' + escapeHtml(paper.journal || "未知期刊") + '"' +
            ' data-search="' + escapeHtml(searchBlob) + '">' +
            '<div class="paper-card-header">' +
            '<div class="paper-head-row">' +
            '<span class="paper-index">' + index + "</span>" +
            '<h3 class="paper-title">' + titleHtml + "</h3>" +
            "</div>" +
            '<div class="paper-meta">' + meta.join('<span class="dot">·</span>') + "</div>" +
            '<div class="tag-row">' + tags + "</div>" +
            "</div>" +
            '<div class="paper-card-content">' +
            (authorText ? '<p class="paper-authors">' + authorText + "</p>" : "") +
            abstractHtml +
            renderAI(paper.AI) +
            "</div>" +
            (links ? '<div class="paper-card-footer"><div class="paper-links">' +
                links + "</div></div>" : "") +
            "</article>";
    }

    function renderPapers() {
        var papers = state.papers;
        if (!papers.length) {
            if (state.rangeView) {
                el.container.innerHTML =
                    '<div class="empty-state"><div class="icon">🗂️</div>' +
                    "<h2>该区间没有论文</h2><p>" +
                    escapeHtml(rangeLabel(state.startDay, state.endDay)) +
                    "（" + state.days.length + " 天）没有匹配到论文。周末与节假日尤其常见，可换一个区间查看。</p></div>";
                return;
            }
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
                '<div class="paper-list paper-container">';
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
        syncJournalLabel();
    }

    /* 期刊过滤器的辅助文案：选中的期刊常被收进浮层里（浮层关掉后就看不到了），
       所以在「期刊」标签上补一个当前筛选提示，避免用户以为筛选没生效。
       手机端 / 矮屏是横向滑动平铺，选中的 chip 本身可见，就不占标签空间了。 */
    function syncJournalLabel() {
        if (!el.journalFilterLabel) { return; }
        var compact = false;
        if (window.matchMedia) {
            compact = window.matchMedia("(max-width: 640px)").matches ||
                window.matchMedia("(max-height: 620px) and (min-width: 641px)").matches;
        }
        var active = state.journal !== "all" && !compact;
        el.journalFilterLabel.textContent = active ? "期刊：" + state.journal : "期刊";
        el.journalFilterLabel.title = active ? state.journal : "";
        el.journalFilterLabel.classList.toggle("filter-label-active", active);
    }

    /* 关掉期刊浮层并恢复按钮文案（元素可能已被重渲染，所以每次现查） */
    function closeJournalPanel() {
        var btn = document.getElementById("moreJournals");
        var panel = document.getElementById("journalMorePanel");
        if (!panel || panel.hidden) { return; }
        panel.hidden = true;
        if (!btn) { return; }
        btn.setAttribute("aria-expanded", "false");
        var total = el.journalFilters.querySelectorAll("[data-filter-journal]").length - 1;
        btn.textContent = "展开全部 " + total + " 个期刊 ▾";
    }

    function bindJournalToggle() {
        var btn = document.getElementById("moreJournals");
        var panel = document.getElementById("journalMorePanel");
        if (!btn || !panel) { return; }
        btn.addEventListener("click", function (event) {
            event.stopPropagation();   // 别让 document 上的「点外部关闭」立刻把它关掉
            var open = panel.hidden;
            panel.hidden = !open;
            btn.setAttribute("aria-expanded", open ? "true" : "false");
            if (open) {
                btn.textContent = "收起期刊列表 ▴";
            } else {
                closeJournalPanel();
            }
        });
        if (journalPanelGlobalBound) { return; }
        journalPanelGlobalBound = true;
        /* 点浮层以外的地方 / 按 Esc 都收起浮层 */
        document.addEventListener("click", function (event) {
            var currentPanel = document.getElementById("journalMorePanel");
            if (!currentPanel || currentPanel.hidden) { return; }
            if (el.journalFilters.contains(event.target)) { return; }
            closeJournalPanel();
        });
        document.addEventListener("keydown", function (event) {
            if (event.key !== "Escape") { return; }
            var currentPanel = document.getElementById("journalMorePanel");
            if (currentPanel && !currentPanel.hidden) { closeJournalPanel(); }
        });
    }

    /* -------------------------------------------------------- 日期切换 */

    function eachNode(nodes, fn) {
        if (!nodes || !nodes.length) { return; }
        Array.prototype.forEach.call(nodes, fn);
    }

    function panelCells() {
        if (!el.datePanel) { return []; }
        return el.datePanel.querySelectorAll(".date-cell");
    }

    function findCell(day) {
        var cells = panelCells();
        for (var i = 0; i < cells.length; i += 1) {
            if (cells[i].getAttribute("data-day") === day) { return cells[i]; }
        }
        return null;
    }

    function closePanel() {
        if (!el.datePanel) { return; }
        el.datePanel.hidden = true;
        if (el.dateToggle) { el.dateToggle.setAttribute("aria-expanded", "false"); }
    }

    /* 清掉面板里所有选中态（含悬停预览），后面的函数再按需重新加 */
    function clearCellMarks() {
        eachNode(panelCells(), function (cell) {
            cell.classList.remove("current");
            cell.classList.remove("in-range");
            cell.classList.remove("range-start");
            cell.classList.remove("range-end");
            cell.classList.remove("preview");
            cell.setAttribute("aria-pressed", "false");
        });
    }

    /* 按当前视图高亮日期格：单日只标 current；区间标 in-range + 两端 range-start/range-end */
    function setActiveDays(days, mode) {
        var list = [];
        (days || []).forEach(function (day) {
            if (isDayString(day)) { list.push(day); }
        });
        var lookup = {};
        list.forEach(function (day) { lookup[day] = true; });
        var sorted = list.slice().sort();
        var earliest = sorted.length ? sorted[0] : null;
        var latest = sorted.length ? sorted[sorted.length - 1] : null;
        var rangeMode = (mode || state.mode) === "range";
        eachNode(panelCells(), function (cell) {
            var day = cell.getAttribute("data-day");
            cell.classList.remove("current");
            cell.classList.remove("in-range");
            cell.classList.remove("range-start");
            cell.classList.remove("range-end");
            cell.classList.remove("preview");
            var active = false;
            if (rangeMode) {
                if (lookup[day]) {
                    cell.classList.add("in-range");
                    if (day === earliest) { cell.classList.add("range-start"); }
                    if (day === latest) { cell.classList.add("range-end"); }
                    active = true;
                }
            } else if (day === state.day) {
                cell.classList.add("current");
                active = true;
            }
            cell.setAttribute("aria-pressed", active ? "true" : "false");
        });
    }

    function restoreActiveDays() {
        var list = state.days && state.days.length ? state.days : (state.day ? [state.day] : []);
        setActiveDays(list, state.rangeView ? "range" : "single");
    }

    function modeButtons() {
        return [el.modeSingle, el.modeRange];
    }

    function modeOfButton(btn) {
        var mode = btn.getAttribute("data-mode");
        if (mode === "single" || mode === "range") { return mode; }
        return btn === el.modeSingle ? "single" : "range";
    }

    function syncModeButtons() {
        modeButtons().forEach(function (btn) {
            if (!btn) { return; }
            var on = modeOfButton(btn) === state.mode;
            if (on) { btn.classList.add("active"); } else { btn.classList.remove("active"); }
            btn.setAttribute("aria-pressed", on ? "true" : "false");
        });
    }

    function updateToggleText() {
        if (!el.dateToggleText) { return; }
        if (state.rangeView && isDayString(state.startDay) && isDayString(state.endDay)) {
            el.dateToggleText.textContent = rangeLabel(state.startDay, state.endDay);
            if (el.dateToggleCount) {
                el.dateToggleCount.textContent = (state.days ? state.days.length : 0) + " 天";
                el.dateToggleCount.hidden = false;
            }
            return;
        }
        el.dateToggleText.textContent = state.day || cfg.defaultDate || "";
        if (el.dateToggleCount) { el.dateToggleCount.hidden = true; }
    }

    function updateHash() {
        if (!(window.history && window.history.replaceState)) { return; }
        if (state.mode === "range" && isDayString(state.startDay) && isDayString(state.endDay)) {
            window.history.replaceState(null, "", "#" + state.startDay + "~" + state.endDay);
            return;
        }
        if (state.day) {
            window.history.replaceState(null, "", "#" + state.day);
        }
    }

    function setHint(text) {
        if (el.dateRangeHint) { el.dateRangeHint.textContent = text; }
    }

    function setDefaultHint() {
        if (state.mode === "range" && pendingStart) {
            setHint("起始 " + dayShort(pendingStart) + "，再点结束日");
            return;
        }
        if (state.mode === "range") {
            setHint("点击日期选择起始日，再点结束日；也可用上方快捷区间");
            return;
        }
        setHint("单日模式：点击日期切换；Shift+点击可从当前日选区间");
    }

    function clearPreview() {
        eachNode(panelCells(), function (cell) { cell.classList.remove("preview"); });
    }

    /* 悬停预览：把 pending ↔ hover 之间的格子标 .preview，并实时给出跨度 */
    function setPreview(endDay) {
        clearPreview();
        if (!pendingStart || !isDayString(endDay)) { return; }
        var earlier = pendingStart <= endDay ? pendingStart : endDay;
        var later = pendingStart <= endDay ? endDay : pendingStart;
        eachNode(panelCells(), function (cell) {
            var day = cell.getAttribute("data-day");
            if (day >= earlier && day <= later) { cell.classList.add("preview"); }
        });
        var span = daysBetween(earlier, later, cfg.days || []).length;
        setHint("起始 " + dayShort(pendingStart) + " → 结束 " + dayShort(endDay) + "（" + span + " 天）");
    }

    function setPendingStart(day) {
        pendingStart = day;
        clearCellMarks();
        var cell = findCell(day);
        if (cell) {
            cell.classList.add("range-start");
            cell.setAttribute("aria-pressed", "true");
        }
        if (el.clearRange) { el.clearRange.hidden = false; }
        setHint("起始 " + dayShort(day) + "，再点结束日");
    }

    /* 只取消待选起点，当前论文视图不动（把视图高亮恢复回来） */
    function clearPendingStart() {
        pendingStart = null;
        clearPreview();
        if (el.clearRange) { el.clearRange.hidden = true; }
        restoreActiveDays();
        setDefaultHint();
    }

    function setMode(mode) {
        if (mode !== "single" && mode !== "range") { mode = "single"; }
        state.mode = mode;
        pendingStart = null;
        if (el.dateRangeBar) { el.dateRangeBar.hidden = mode !== "range"; }
        if (el.clearRange) { el.clearRange.hidden = true; }
        syncModeButtons();
        restoreActiveDays();
        setDefaultHint();
    }

    function applyPreset(count) {
        var n = parseInt(count, 10);
        if (!n || n < 1) { return; }
        var days = (cfg.days || []).slice(0, n);
        if (!days.length) { return; }
        closePanel();
        switchRange(days[days.length - 1], days[0]);
    }

    function switchDay(day, options) {
        options = options || {};
        if (!day) { return; }
        loadToken += 1;
        var token = loadToken;
        state.mode = "single";
        state.rangeView = false;
        state.day = day;
        state.startDay = day;
        state.endDay = day;
        state.days = [day];
        state.topic = "all";
        state.journal = "all";
        state.query = "";
        pendingStart = null;
        if (el.search) { el.search.value = ""; }
        syncModeButtons();
        if (el.dateRangeBar) { el.dateRangeBar.hidden = true; }
        if (el.clearRange) { el.clearRange.hidden = true; }
        if (el.chipDaysWrap) { el.chipDaysWrap.hidden = true; }
        setActiveDays([day], "single");
        setDefaultHint();
        updateToggleText();
        if (options.updateHash !== false) { updateHash(); }
        el.container.innerHTML =
            '<div class="loading-state"><div class="loading-spinner"></div><p>正在加载 ' +
            escapeHtml(day) + " 的论文…</p></div>";
        loadDay(day).then(function (papers) {
            if (token !== loadToken) { return; }   // 已经切到别的视图，丢弃旧结果
            state.papers = papers;
            renderChipCounts(papers);
            renderTopicFilters(papers);
            renderJournalFilters(papers);
            renderPapers();
        }).catch(function (err) {
            if (token !== loadToken) { return; }   // 同上：旧请求失败也不干扰新视图
            el.container.innerHTML =
                '<div class="empty-state"><div class="icon">⚠️</div><h2>加载失败</h2>' +
                "<p>" + escapeHtml(day) + " 的数据读取失败：" + escapeHtml(err.message) +
                '。请检查该日期的数据文件是否已发布。</p></div>';
        });
    }

    /* 有失败日时在结果顶部插一条警告；全部失败则整块走错误空状态 */
    function prependLoadWarning(failedDays) {
        var note = document.createElement("div");
        note.className = "load-warning";
        note.innerHTML = "⚠️ 有 " + failedDays.length + " 天数据加载失败：" +
            escapeHtml(failedDays.join("、")) + "。其余日期的论文已正常合并展示。";
        el.container.insertBefore(note, el.container.firstChild);
    }

    function applyRangeResults(results) {
        var merged = [];
        var failedDays = [];
        results.forEach(function (item) {
            if (item.error) {
                failedDays.push(item.day);
                return;
            }
            item.papers.forEach(function (paper) {
                var copy = {};
                Object.keys(paper).forEach(function (key) { copy[key] = paper[key]; });
                copy.__day = item.day;
                merged.push(copy);
            });
        });
        /* results 顺序 = state.days 的顺序（日期倒序、最新在前），
           dedupePapers 保留首次出现，所以留下的是最新收录日的那条。 */
        var unique = dedupePapers(merged);
        state.papers = sortRangePapers(unique, TOPIC_ORDER);

        if (failedDays.length === state.days.length) {
            renderChipCounts([]);
            renderTopicFilters([]);
            renderJournalFilters([]);
            el.container.innerHTML =
                '<div class="empty-state"><div class="icon">⚠️</div><h2>加载失败</h2>' +
                "<p>" + escapeHtml(failedDays.join("、")) +
                " 这几天的数据全部读取失败，请检查数据文件是否已发布。</p></div>";
            return;
        }

        renderChipCounts(state.papers);
        renderTopicFilters(state.papers);
        renderJournalFilters(state.papers);
        renderPapers();
        if (failedDays.length) { prependLoadWarning(failedDays); }
    }

    function switchRange(start, end, options) {
        options = options || {};
        var days = daysBetween(start, end, cfg.days || []);
        if (!days.length) { return; }
        loadToken += 1;
        var token = loadToken;
        state.mode = "range";
        state.rangeView = true;
        state.days = days;
        state.startDay = days[days.length - 1];
        state.endDay = days[0];
        state.day = days[0];
        state.topic = "all";
        state.journal = "all";
        state.query = "";
        pendingStart = null;
        if (el.search) { el.search.value = ""; }
        syncModeButtons();
        if (el.dateRangeBar) { el.dateRangeBar.hidden = false; }
        if (el.clearRange) { el.clearRange.hidden = true; }
        setActiveDays(days, "range");
        setDefaultHint();
        updateToggleText();
        if (el.chipDays) { el.chipDays.textContent = days.length; }
        if (el.chipDaysWrap) { el.chipDaysWrap.hidden = false; }
        if (options.updateHash !== false) { updateHash(); }
        el.container.innerHTML =
            '<div class="loading-state"><div class="loading-spinner"></div><p>正在加载 ' +
            escapeHtml(rangeLabel(state.startDay, state.endDay)) +
            "（" + days.length + " 天）的论文…</p></div>";
        Promise.all(loadDays(days)).then(function (results) {
            if (token !== loadToken) { return; }   // 慢请求后到：别覆盖新视图
            applyRangeResults(results);
        });
    }

    /* ------------------------------------------------------------ 绑定 */

    function bindModeButtons() {
        modeButtons().forEach(function (btn) {
            if (!btn) { return; }
            btn.addEventListener("click", function (event) {
                event.stopPropagation();
                setMode(modeOfButton(btn));
            });
        });
    }

    function bindRangeTools() {
        eachNode(document.querySelectorAll(".date-range-preset"), function (btn) {
            btn.addEventListener("click", function (event) {
                event.stopPropagation();
                applyPreset(btn.getAttribute("data-preset"));
            });
        });
        if (el.clearRange) {
            el.clearRange.addEventListener("click", function (event) {
                event.stopPropagation();
                clearPendingStart();
            });
        }
    }

    function initDatePicker() {
        if (el.dateToggle && el.datePanel) {
            el.dateToggle.addEventListener("click", function (event) {
                event.stopPropagation();
                var open = el.datePanel.hidden;
                el.datePanel.hidden = !open;
                el.dateToggle.setAttribute("aria-expanded", open ? "true" : "false");
                if (open) {
                    var cur = el.datePanel.querySelector(
                        ".date-cell.current, .date-cell.range-start, .date-cell.range-end"
                    );
                    if (cur && cur.scrollIntoView) { cur.scrollIntoView({ block: "nearest" }); }
                }
            });
        }
        if (el.datePanel) {
            el.datePanel.addEventListener("click", function (event) {
                var cell = event.target.closest ? event.target.closest(".date-cell") : null;
                if (!cell) {
                    event.stopPropagation();
                    return;
                }
                event.stopPropagation();
                var day = cell.getAttribute("data-day");
                if (!isDayString(day)) { return; }
                if (state.mode === "range") {
                    if (!pendingStart) {
                        /* 第一次点：只记住起点，面板不关，可继续看/点结束日 */
                        setPendingStart(day);
                        return;
                    }
                    var earlier = pendingStart <= day ? pendingStart : day;
                    var later = pendingStart <= day ? day : pendingStart;
                    pendingStart = null;
                    closePanel();
                    /* 起止点相同 = 用户只想看这一天：回到单日视图，
                       避免出现「1 天区间」这种既像单日又带区间徽标的中间态 */
                    if (earlier === later) {
                        switchDay(day);
                        return;
                    }
                    switchRange(earlier, later);
                    return;
                }
                closePanel();
                if (event.shiftKey && isDayString(state.day)) {
                    /* 单日模式下 Shift+点击 = 从当前日到点击日的区间 */
                    var from = state.day <= day ? state.day : day;
                    var to = state.day <= day ? day : state.day;
                    if (from === to) {
                        switchDay(day);
                        return;
                    }
                    switchRange(from, to);
                    return;
                }
                switchDay(day);
            });
            el.datePanel.addEventListener("mouseover", function (event) {
                if (state.mode !== "range" || !pendingStart) { return; }
                var cell = event.target.closest ? event.target.closest(".date-cell") : null;
                if (!cell) { return; }
                var day = cell.getAttribute("data-day");
                if (isDayString(day)) { setPreview(day); }
            });
            el.datePanel.addEventListener("mouseleave", function () {
                if (!pendingStart) { return; }
                clearPreview();
                setDefaultHint();
            });
        }
        document.addEventListener("click", function () {
            closePanel();
        });
        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && el.datePanel && !el.datePanel.hidden) {
                closePanel();
                if (el.dateToggle) { el.dateToggle.focus(); }
            }
        });
        var latest = document.getElementById("jumpLatest");
        if (latest) {
            latest.addEventListener("click", function (event) {
                event.stopPropagation();
                closePanel();
                switchDay(cfg.days[0]);
            });
        }
        bindModeButtons();
        bindRangeTools();
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
            closeJournalPanel();   // 选完就收起浮层，正文立刻可见
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
        /* 支持 #YYYY-MM-DD 直达单日、#YYYY-MM-DD~YYYY-MM-DD 直达区间（可分享、刷新不丢） */
        var parsed = parseHash(location.hash);
        var knownDays = cfg.days || [];
        if (parsed && parsed.mode === "range" &&
                daysBetween(parsed.start, parsed.end, knownDays).length) {
            switchRange(parsed.start, parsed.end, { updateHash: false });
            return;
        }
        if (parsed && parsed.mode === "single" && knownDays.indexOf(parsed.day) >= 0) {
            switchDay(parsed.day, { updateHash: false });
            return;
        }
        switchDay(cfg.defaultDate, { updateHash: false });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
