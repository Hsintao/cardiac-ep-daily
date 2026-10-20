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
    var cache = {};    // day -> Promise 或 papers[]（在途请求也缓存，避免重复并发 fetch）
    var cardIndex = [];    // 卡片检索索引 [{card, blob, topic, journal, group}]，替代 data-search 属性
    var pendingStart = null; // 区间模式里已点下的起始日，等待点结束日
    var lastPreviewDay = null;  // 悬停预览去重：同一天反复 mouseover 不重算
    var loadToken = 0;       // 视图切换序号：慢请求回来时若已切走就丢弃，避免旧结果覆盖新视图
    var journalPanelGlobalBound = false; // 期刊浮层的全局事件只绑一次（重渲染会换元素）
    var panelCellsCache = null;  // 日期面板格子构建后不再变，查一次缓存复用
    var mqlNarrow = null;    // 窄屏媒体查询：按需创建一次后复用
    var mqlShort = null;     // 矮屏媒体查询：同上

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
        srStatus: document.getElementById("srStatus"),
    };

    /* 状态播报：切日期 / 筛选结果变化时写入 aria-live 区域，通知屏幕阅读器。
       先清空再写入，保证连续两次相同文本也会被重新播报。 */
    function announce(text) {
        if (!el.srStatus) { return; }
        el.srStatus.textContent = "";
        el.srStatus.textContent = String(text);
    }

    /* 主题顺序由构建端写进 site-config（数据源 render/build_html.py 的 TOPIC_ORDER），
       前端不再自留一份，避免两份清单漂移；cfg 缺失时退回内置默认。 */
    var TOPIC_ORDER = (Array.isArray(cfg.topics) && cfg.topics.length) ? cfg.topics :
        ["房颤与消融", "室性心律失常", "器械治疗", "影像与标测", "遗传与基础", "其他"];

    /* ---------------------------------------------------------------- 工具 */

    var ESCAPE_HTML_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

    function escapeHtml(value) {
        if (value === null || value === undefined) { return ""; }
        /* 单次正则 + 映射表：逐字符替换，比串 5 次 replace 快（大区间要走数万次） */
        return String(value).replace(/[&<>"']/g, function (ch) { return ESCAPE_HTML_MAP[ch]; });
    }

    /* 链接同样要过一遍：只放行 http(s)/mailto，避免 javascript: 之类伪协议进入 href。
       协议白名单与构建端 render/build_html.py 的 _SAFE_SCHEMES 保持一致。 */
    function safeUrl(url) {
        if (!url) { return ""; }
        var text = String(url).trim();
        if (/^(https?:\/\/|mailto:)/i.test(text)) { return escapeHtml(text); }
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

    /* 先缓存「在途 Promise」再等人：快速连点同一天（或预设区间与单日重叠）
       时复用同一个请求，不会打出重复的并发 fetch；成功后换成结果数组，
       失败则清掉缓存，下次重试还能再请求。 */
    function loadDay(day) {
        var hit = cache[day];
        if (hit) { return Promise.resolve(hit); }
        var pending = fetch(dataUrlFor(day), { cache: "no-cache" })
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
        cache[day] = pending;
        pending.catch(function () {
            if (cache[day] === pending) { delete cache[day]; }
        });
        return pending;
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
        /* Object.create(null)：无原型键，数据来源的键名撞上
           Object.prototype 成员（如 "constructor"）也不会读出奇怪的值 */
        var counts = Object.create(null);
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
        /* 期刊名来自数据：必须用无原型对象计数，否则名为 "constructor"
           之类的期刊会让计数和排序读出 Object.prototype 成员 */
        var counts = Object.create(null);
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
        /* 五段标签来自 site-config（构建端 render/build_html.py 的 AI_SECTIONS），
           cfg 缺失时退回内置默认 */
        var sections = (Array.isArray(cfg.aiSections) && cfg.aiSections.length) ? cfg.aiSections : [
            ["tldr", "一句话结论"],
            ["motivation", "研究动机"],
            ["method", "方法"],
            ["result", "结果"],
            ["conclusion", "结论与意义"],
        ];
        /* 一句话结论常驻展开（信息密度最高，值得直接看到）；
           其余四段（动机/方法/结果/结论）收进 <details>，默认折叠。
           两列布局下卡片本来就高，折叠后一屏能看更多篇。 */
        /* 标题圆点由 CSS 的 .ai-panel-head::before 提供，这里不再硬编码「●」，
           否则深浅两个圆点并存 */
        var html = '<div class="ai-panel"><div class="ai-panel-head">AI 中文解读</div>';
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

    /* 关键词检索串：用原始字段拼 —— 完整作者名单（不是页面上截断 + 带 HTML
       标记的那份显示文本），并带上 PMID / DOI / 主题 / 文献类型，卡片上看得见
       的都应该搜得到。__day 也进检索串：区间视图里可按「2026-10-05」搜收录日。
       这份文本只留在内存索引里（见 buildCardIndex），不再写进 data-search 属性，
       避免同一段全文在 DOM 里再存一份。 */
    function searchBlobFor(paper) {
        var authors = Array.isArray(paper.authors) ? paper.authors : [];
        var pubTypes = Array.isArray(paper.pub_types) ? paper.pub_types : [];
        return [
            paper.title, authors.join(" "), paper.summary, paper.summary_zh,
            paper.journal, paper.pmid, paper.doi, paper.topic,
            pubTypes.join(" "), paper.__day
        ].join(" ").toLowerCase();
    }

    function renderPaper(paper, index) {
        /* authors 契约是数组，但异常数据可能是字符串或缺失，先兜底；
           每位作者都必须过 escapeHtml —— 原始拼接会把数据里的标签带进 DOM */
        var authors = Array.isArray(paper.authors) ? paper.authors : [];
        var authorText = authors.slice(0, 3).map(function (name) { return escapeHtml(name); }).join("、");
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

        /* 卡片结构对齐参考项目 daily-arXiv-ai-enhanced：
              paper-card
                ├── paper-card-header   ← 标题 / 期刊·日期 / 标签
                ├── paper-card-content  ← 作者 / 摘要 / AI 解读
                └── paper-card-footer   ← 链接按钮（对应参考项目的 card-footer）
           类名 paper-title / paper-meta / tag-row / paper-authors / abstract-block /
           ai-panel / paper-links 全部保留，供 assets/style.css 匹配。 */
        return '<article class="paper-card"' +
            ' data-topic="' + escapeHtml(paper.topic || "其他") + '"' +
            ' data-journal="' + escapeHtml(paper.journal || "未知期刊") + '">' +
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
            cardIndex = [];
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
        var groups = Object.create(null);
        papers.forEach(function (p) {
            var t = TOPIC_ORDER.indexOf(p.topic) >= 0 ? p.topic : "其他";
            (groups[t] = groups[t] || []).push(p);
        });
        /* 每个主题组一段 HTML；卡片的 DOM 顺序与 blobs 顺序一一对应
           （组序 = TOPIC_ORDER，组内序 = papers 序），渲染完 zip 成检索索引。 */
        var sections = [];
        var blobs = [];
        var index = 0;
        TOPIC_ORDER.forEach(function (topic) {
            var list = groups[topic];
            if (!list || !list.length) { return; }
            var html = '<section class="topic-group" data-group-topic="' + escapeHtml(topic) + '">' +
                '<h2 class="topic-heading"><span class="bar"></span><span>' + escapeHtml(topic) +
                '</span><span class="topic-count">' + list.length + " 篇</span></h2>" +
                '<div class="paper-list paper-container">';
            list.forEach(function (p) {
                index += 1;
                html += renderPaper(p, index);
                blobs.push({
                    blob: searchBlobFor(p),
                    topic: p.topic || "其他",
                    journal: p.journal || "未知期刊",
                    group: topic
                });
            });
            html += "</div></section>";
            sections.push(html);
        });
        renderSections(sections, blobs);
    }

    /* 大区间（上千篇、数 MB HTML）一次 innerHTML 会长时间阻塞主线程：
       先同步挂上第一组让首屏立刻可见，其余组按 ~12ms 时间盒逐批追加。
       期间用户切换视图（loadToken 变化）就放弃剩余追加，避免覆盖新视图。 */
    function renderSections(sections, blobs) {
        var token = loadToken;
        el.container.innerHTML = sections[0];
        var pos = 1;
        function finish() {
            buildCardIndex(blobs);
            applyFilters();
            bindJournalToggle();
        }
        if (sections.length === 1) {
            finish();
            return;
        }
        function step() {
            if (token !== loadToken) { return; }
            var deadline = Date.now() + 12;
            while (pos < sections.length) {
                el.container.insertAdjacentHTML("beforeend", sections[pos]);
                pos += 1;
                if (Date.now() >= deadline) { break; }
            }
            if (pos < sections.length) {
                setTimeout(step, 0);
            } else {
                finish();
            }
        }
        setTimeout(step, 0);
    }

    /* 检索 / 筛选用的内存索引：全文只留在 JS 堆里这一份，不再复制进
       data-search 属性（否则同一段文本在 DOM 里存两份，筛选还要逐个
       getAttribute 解码）。 */
    function buildCardIndex(blobs) {
        cardIndex = [];
        var cards = el.container.querySelectorAll(".paper-card");
        for (var i = 0; i < cards.length; i += 1) {
            var meta = blobs[i] || { blob: "", topic: "", journal: "", group: "" };
            cardIndex.push({
                card: cards[i],
                blob: meta.blob,
                topic: meta.topic,
                journal: meta.journal,
                group: meta.group
            });
        }
    }

    /* ------------------------------------------------------------ 筛选 */

    function applyFilters() {
        var q = state.query.trim().toLowerCase();
        var visible = 0;
        var groupVisible = Object.create(null);
        cardIndex.forEach(function (entry) {
            var show =
                (state.topic === "all" || entry.topic === state.topic) &&
                (state.journal === "all" || entry.journal === state.journal) &&
                (!q || entry.blob.indexOf(q) >= 0);
            entry.card.style.display = show ? "" : "none";
            if (show) {
                visible += 1;
                groupVisible[entry.group] = true;
            }
        });
        // 整组都被筛掉时隐藏组标题，避免出现空标题
        var sections = el.container.querySelectorAll(".topic-group");
        Array.prototype.forEach.call(sections, function (section) {
            section.style.display = groupVisible[section.getAttribute("data-group-topic")] ? "" : "none";
        });
        var emptyNote = document.getElementById("filterEmptyNote");
        if (visible === 0 && cardIndex.length > 0) {
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
        /* 有搜索词时播报剩余篇数，屏幕阅读器用户不必逐张数卡片 */
        if (q) { announce("关键词筛选后剩余 " + visible + " 篇论文"); }
        syncJournalLabel();
    }

    /* 期刊过滤器的辅助文案：选中的期刊常被收进浮层里（浮层关掉后就看不到了），
       所以在「期刊」标签上补一个当前筛选提示，避免用户以为筛选没生效。
       手机端 / 矮屏是横向滑动平铺，选中的 chip 本身可见，就不占标签空间了。 */
    function isCompactLayout() {
        if (!window.matchMedia) { return false; }
        if (!mqlNarrow) { mqlNarrow = window.matchMedia("(max-width: 640px)"); }
        if (!mqlShort) { mqlShort = window.matchMedia("(max-height: 620px) and (min-width: 641px)"); }
        return mqlNarrow.matches || mqlShort.matches;
    }

    function syncJournalLabel() {
        if (!el.journalFilterLabel) { return; }
        var active = state.journal !== "all" && !isCompactLayout();
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
        /* 面板格子是构建时静态渲染的，永不重建：查一次后缓存复用，
           否则每次悬停预览 / 高亮刷新都重新 querySelectorAll */
        if (!panelCellsCache && el.datePanel) {
            panelCellsCache = el.datePanel.querySelectorAll(".date-cell");
        }
        return panelCellsCache || [];
    }

    function findCell(day) {
        var cells = panelCells();
        for (var i = 0; i < cells.length; i += 1) {
            if (cells[i].getAttribute("data-day") === day) { return cells[i]; }
        }
        return null;
    }

    /* 网格漫游（roving tabindex）：面板里只有一格在 Tab 序列里（tabindex=0），
       焦点落到哪格哪格进 Tab 序列；方向键在格子间移动（←/→ 前后一天，
       ↑/↓ 前后一周（近似），Home/End 跳首尾），与 ARIA grid 模式一致。 */
    function initCellGridNav() {
        var cells = panelCells();
        if (!cells.length) { return; }
        var current = el.datePanel.querySelector(
            ".date-cell.current, .date-cell.range-start, .date-cell.range-end"
        ) || cells[0];
        eachNode(cells, function (cell) {
            cell.setAttribute("tabindex", cell === current ? "0" : "-1");
        });
        el.datePanel.addEventListener("focusin", function (event) {
            var cell = event.target.closest ? event.target.closest(".date-cell") : null;
            if (!cell) { return; }
            eachNode(panelCells(), function (c) {
                c.setAttribute("tabindex", c === cell ? "0" : "-1");
            });
        });
        el.datePanel.addEventListener("keydown", function (event) {
            var cell = event.target.closest ? event.target.closest(".date-cell") : null;
            if (!cell) { return; }
            var all = panelCells();
            var index = Array.prototype.indexOf.call(all, cell);
            if (index < 0) { return; }
            var delta = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[event.key];
            var next = -1;
            if (delta) {
                /* 月份各自成格、首日不对齐星期，±7 只是近似「上/下周」，越界夹在首尾 */
                next = Math.min(Math.max(index + delta, 0), all.length - 1);
            } else if (event.key === "Home") {
                next = 0;
            } else if (event.key === "End") {
                next = all.length - 1;
            } else {
                return;
            }
            event.preventDefault();
            if (next !== index) { all[next].focus(); }
        });
    }

    /* restoreFocus=true 用于「用户在面板里完成操作」的关闭（选好日期、按 Esc）：
       焦点还给触发按钮，键盘用户不会丢位置。document 全局点击关闭时不传，
       避免鼠标点别处反而把焦点抢回顶栏。 */
    function closePanel(restoreFocus) {
        if (!el.datePanel) { return; }
        el.datePanel.hidden = true;
        if (el.dateToggle) { el.dateToggle.setAttribute("aria-expanded", "false"); }
        if (restoreFocus && el.dateToggle) { el.dateToggle.focus(); }
    }

    /* 清掉面板里所有选中态（含悬停预览），后面的函数再按需重新加。
       格子不是开关按钮：选中态用 aria-current="date" 表达，不用 aria-pressed。 */
    function clearCellMarks() {
        eachNode(panelCells(), function (cell) {
            cell.classList.remove("current");
            cell.classList.remove("in-range");
            cell.classList.remove("range-start");
            cell.classList.remove("range-end");
            cell.classList.remove("preview");
            cell.removeAttribute("aria-current");
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
            if (active) {
                cell.setAttribute("aria-current", "date");
            } else {
                cell.removeAttribute("aria-current");
            }
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

    /* 写地址栏 hash：用 location.hash 赋值（产生历史记录，前进/后退可用）。
       赋值会触发 hashchange；onHashChange 里按 state 去重，自己写回的
       那次会被识别为「已是当前视图」而忽略，不会二次加载。 */
    function updateHash() {
        var next = "";
        if (state.rangeView && isDayString(state.startDay) && isDayString(state.endDay)) {
            next = "#" + state.startDay + "~" + state.endDay;
        } else if (state.day) {
            next = "#" + state.day;
        }
        if (!next || location.hash === next) { return; }
        location.hash = next;
    }

    /* 浏览器前进/后退、手工改地址栏：把 hash 还原成对应视图。
       与当前视图一致的 hash（自己写回的那次）直接忽略。 */
    function onHashChange() {
        var parsed = parseHash(location.hash);
        if (!parsed) {
            /* hash 为空（回到「无 hash」的初始历史条目，或被手工清空）：
               地址栏没有日期信息了，视图回到默认最新一天，保持 URL 与视图一致 */
            var fallback = cfg.defaultDate;
            if (fallback && (state.rangeView || state.day !== fallback)) {
                switchDay(fallback, { updateHash: false });
            }
            return;
        }
        var knownDays = cfg.days || [];
        if (parsed.mode === "range") {
            if (state.rangeView && state.startDay === parsed.start && state.endDay === parsed.end) {
                return;
            }
            if (daysBetween(parsed.start, parsed.end, knownDays).length) {
                switchRange(parsed.start, parsed.end, { updateHash: false });
            }
            return;
        }
        if (!state.rangeView && state.day === parsed.day) { return; }
        if (knownDays.indexOf(parsed.day) >= 0) {
            switchDay(parsed.day, { updateHash: false });
        }
    }

    /* 切换日期/区间后回到顶部：长列表里原地替换内容，不停留在
       之前滚到的中部位置，否则容易误判「没切换成功」 */
    function scrollToTop() {
        if (window.scrollTo) { window.scrollTo(0, 0); }
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
        lastPreviewDay = null;
        clearCellMarks();
        var cell = findCell(day);
        if (cell) {
            cell.classList.add("range-start");
            cell.setAttribute("aria-current", "date");
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
        clearPreview();
        if (el.dateRangeBar) { el.dateRangeBar.hidden = mode !== "range"; }
        if (el.clearRange) { el.clearRange.hidden = true; }
        syncModeButtons();
        if (mode === "single") {
            /* 从区间视图切回单日：直接落到区间内最新一天的单日结果。
               否则正文还是区间、日期面板里整段区间高亮也留着，看起来就像
               「单日」被区间高亮盖住了。 */
            if (state.rangeView) {
                switchDay(state.day || cfg.defaultDate);
                return;
            }
            restoreActiveDays();
            setDefaultHint();
            return;
        }
        /* 切到区间：当前正文保持不变。已经在区间视图时恢复区间高亮；
           单日视图则清掉单日高亮，等待用户重新点选起始日（不预设起点，
           避免用户第一次点其它日期时被当成结束日，意外选出一大段区间）。 */
        if (state.rangeView) {
            restoreActiveDays();
        } else {
            clearCellMarks();
        }
        setDefaultHint();
    }

    function applyPreset(preset) {
        var all = cfg.days || [];
        var days;
        if (preset === "all") {
            days = all.slice();
        } else {
            var n = parseInt(preset, 10);
            if (!n || n < 1) { return; }
            days = all.slice(0, n);
        }
        if (!days.length) { return; }
        closePanel(true);
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
        cardIndex = [];
        loadDay(day).then(function (papers) {
            if (token !== loadToken) { return; }   // 已经切到别的视图，丢弃旧结果
            state.papers = papers;
            renderChipCounts(papers);
            renderTopicFilters(papers);
            renderJournalFilters(papers);
            renderPapers();
            announce(day + "：" + papers.length + " 篇论文");
            if (options.scroll !== false) { scrollToTop(); }
        }).catch(function (err) {
            if (token !== loadToken) { return; }   // 同上：旧请求失败也不干扰新视图
            el.container.innerHTML =
                '<div class="empty-state"><div class="icon">⚠️</div><h2>加载失败</h2>' +
                "<p>" + escapeHtml(day) + " 的数据读取失败：" + escapeHtml(err.message) +
                '。请检查该日期的数据文件是否已发布。</p></div>';
            announce(day + " 的数据读取失败");
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
                merged.push(Object.assign({}, paper, { __day: item.day }));
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
        cardIndex = [];
        Promise.all(loadDays(days)).then(function (results) {
            if (token !== loadToken) { return; }   // 慢请求后到：别覆盖新视图
            applyRangeResults(results);
            announce(rangeLabel(state.startDay, state.endDay) +
                "：" + state.papers.length + " 篇论文");
            if (options.scroll !== false) { scrollToTop(); }
        }).catch(function (err) {
            /* loadDays 每天各自 catch，不会整体拒绝；能走到这里的是
               applyRangeResults 等渲染阶段的异常 —— 不兜底就会永久停在加载态 */
            if (token !== loadToken) { return; }
            el.container.innerHTML =
                '<div class="empty-state"><div class="icon">⚠️</div><h2>加载失败</h2>' +
                "<p>" + escapeHtml(rangeLabel(state.startDay, state.endDay)) +
                " 的数据处理失败：" + escapeHtml(err && err.message ? err.message : String(err)) +
                "。请稍后重试。</p></div>";
            announce(rangeLabel(state.startDay, state.endDay) + " 的数据处理失败");
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
                    ) || panelCells()[0];
                    if (cur) {
                        if (cur.scrollIntoView) { cur.scrollIntoView({ block: "nearest" }); }
                        /* 打开面板即聚焦当前选中格：键盘用户立刻能用方向键漫游，
                           不必先 Tab 穿过大半个面板 */
                        if (cur.focus) { cur.focus({ preventScroll: true }); }
                    }
                }
            });
            initCellGridNav();
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
                    closePanel(true);
                    /* 起止点相同 = 用户只想看这一天：回到单日视图，
                       避免出现「1 天区间」这种既像单日又带区间徽标的中间态 */
                    if (earlier === later) {
                        switchDay(day);
                        return;
                    }
                    switchRange(earlier, later);
                    return;
                }
                closePanel(true);
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
                /* 同一格子上反复 mouseover 不重算（每次都要清/刷一整段格子的类名） */
                if (!isDayString(day) || day === lastPreviewDay) { return; }
                lastPreviewDay = day;
                setPreview(day);
            });
            el.datePanel.addEventListener("mouseleave", function () {
                if (!pendingStart) { return; }
                lastPreviewDay = null;
                clearPreview();
                setDefaultHint();
            });
        }
        document.addEventListener("click", function () {
            closePanel();
        });
        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && el.datePanel && !el.datePanel.hidden) {
                closePanel(true);
            }
        });
        var latest = document.getElementById("jumpLatest");
        if (latest) {
            latest.addEventListener("click", function (event) {
                event.stopPropagation();
                closePanel(true);
                var latestDay = (cfg.days || [])[0] || cfg.defaultDate;
                if (latestDay) { switchDay(latestDay); }
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
        /* 150ms 防抖：大区间上千张卡片时，逐键触发全量筛选会连续重排 */
        var searchTimer = null;
        el.search.addEventListener("input", function () {
            state.query = el.search.value;
            if (searchTimer) { clearTimeout(searchTimer); }
            searchTimer = setTimeout(applyFilters, 150);
        });
    }

    function initTheme() {
        var btn = document.getElementById("themeToggle");
        if (!btn) { return; }
        var root = document.documentElement;
        var mql = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
        /* <html data-theme> 由页头内联脚本保证总有值（手动选择或跟随系统） */
        function current() {
            var attr = root.getAttribute("data-theme");
            if (attr === "dark" || attr === "light") { return attr; }
            return (mql && mql.matches) ? "dark" : "light";
        }
        /* 有没有手动选择过要看 localStorage：初始化脚本总是写 data-theme，
           属性本身已不再区分手动/跟随 */
        function isManual() {
            try {
                var saved = localStorage.getItem("ep-digest-theme");
                return saved === "dark" || saved === "light";
            } catch (e) { return false; }
        }
        function label() {
            var suffix = isManual() ? "" : "（跟随系统）";
            btn.textContent = (current() === "dark" ? "☀️ 浅色" : "🌙 深色") + suffix;
        }
        btn.addEventListener("click", function () {
            var next = current() === "dark" ? "light" : "dark";
            root.setAttribute("data-theme", next);
            try { localStorage.setItem("ep-digest-theme", next); } catch (e) {}
            label();
        });
        /* 未手动选择时跟随系统主题实时切换 */
        if (mql && mql.addEventListener) {
            mql.addEventListener("change", function (event) {
                if (isManual()) { return; }
                root.setAttribute("data-theme", event.matches ? "dark" : "light");
                label();
            });
        }
        label();
    }

    /* 打印前强制展开所有折叠块（闭合的 <details> 只显示 summary，摘要/解读会丢），
       打印后还原回用户原本的折叠状态 */
    function initPrintExpand() {
        if (!window.addEventListener) { return; }
        window.addEventListener("beforeprint", function () {
            document.querySelectorAll("details:not([open])").forEach(function (d) {
                d.setAttribute("open", "");
                d.setAttribute("data-print-opened", "");
            });
        });
        window.addEventListener("afterprint", function () {
            document.querySelectorAll("details[data-print-opened]").forEach(function (d) {
                d.removeAttribute("open");
                d.removeAttribute("data-print-opened");
            });
        });
    }

    function init() {
        initDatePicker();
        initFilters();
        initTheme();
        initPrintExpand();
        /* logo = 回到最新一天。不加 handler 时 href="#" 会把地址栏里
           可分享的日期 hash 清掉（URL 与视图对不上） */
        var logo = document.querySelector(".site-logo");
        if (logo) {
            logo.addEventListener("click", function (event) {
                event.preventDefault();
                var latestDay = (cfg.days || [])[0] || cfg.defaultDate;
                if (latestDay) { switchDay(latestDay); }
            });
        }
        /* 前进/后退、手工改地址栏时把 hash 还原成视图 */
        window.addEventListener("hashchange", onHashChange);
        /* 支持 #YYYY-MM-DD 直达单日、#YYYY-MM-DD~YYYY-MM-DD 直达区间（可分享、刷新不丢）。
           首次加载不滚动：让浏览器自己的滚动恢复（刷新场景）生效 */
        var parsed = parseHash(location.hash);
        var knownDays = cfg.days || [];
        if (parsed && parsed.mode === "range" &&
                daysBetween(parsed.start, parsed.end, knownDays).length) {
            switchRange(parsed.start, parsed.end, { updateHash: false, scroll: false });
            return;
        }
        if (parsed && parsed.mode === "single" && knownDays.indexOf(parsed.day) >= 0) {
            switchDay(parsed.day, { updateHash: false, scroll: false });
            return;
        }
        switchDay(cfg.defaultDate, { updateHash: false, scroll: false });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
