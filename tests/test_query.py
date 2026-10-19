"""检索式构造的测试。

这一层是「实测结论」的守卫：日期字段必须是 [edat]、不能出现 [dp]/[MeSH]。
任何人把它改回旧写法，这里必须立刻变红。

所有测试都是纯字符串运算，**不打网络**。
"""

from __future__ import annotations

from datetime import date

import pytest

from daily_ep.config import Config
from daily_ep.sources import europepmc
from daily_ep.sources.pubmed import (
    build_clause,
    build_date_clause,
    build_query,
    date_range,
    efetch_params,
    esearch_params,
)

TODAY = date(2026, 10, 19)


@pytest.fixture()
def cfg() -> Config:
    """从仓库真实 config.yaml 读配置，保证测试盯的是生产用的那套词表。"""
    return Config.load()


# ---------------------------------------------------------------------------
# 日期窗口
# ---------------------------------------------------------------------------

def test_date_range_covers_today_and_lookback():
    window = date_range(2, TODAY)
    assert window == [date(2026, 10, 18), date(2026, 10, 19)]


def test_date_range_single_day():
    assert date_range(1, TODAY) == [TODAY]


def test_date_range_never_empty():
    # days=0 或负数也不该产生空窗口（否则会退化成全库检索）
    assert date_range(0, TODAY) == [TODAY]
    assert date_range(-5, TODAY) == [TODAY]


# ---------------------------------------------------------------------------
# 核心断言：[edat] 且不含 [dp] / [MeSH]
# ---------------------------------------------------------------------------

def test_query_uses_edat_date_field(cfg):
    query = build_query(cfg.journals, cfg.topics, date_range(2, TODAY), cfg.date_field)
    assert '"2026/10/19"[edat]' in query
    assert '"2026/10/18"[edat]' in query


def test_query_never_contains_dp_or_mesh(cfg):
    query = build_query(cfg.journals, cfg.topics, date_range(2, TODAY), cfg.date_field)
    # [dp] 是印刷日期，在版文章常为未来月份，做日报会漏掉当天内容
    assert "[dp]" not in query
    assert '"2026/10/19"[dp]' not in query
    # [MeSH] 对新文章（未标引）几乎零命中
    assert "[MeSH]" not in query
    assert "[mesh]" not in query.lower().replace("[mesh]", "[MeSH]")


def test_config_date_field_is_edat(cfg):
    # config.yaml 被改动时必须立刻发现
    assert cfg.date_field == "edat"


def test_real_config_query_shape(cfg):
    """对着真实 config.yaml 断言整体形状。"""
    query = build_query(cfg.journals, cfg.topics, date_range(2, TODAY), cfg.date_field)

    # 期刊白名单逐个出现，且是 [jour]（单词期刊名不加引号，多词的加）
    for journal in cfg.journals:
        assert f"{journal}[jour]" in query or f'"{journal}"[jour]' in query

    # 主题词逐个出现，且是 [tiab]（多词短语加引号）
    for topic in cfg.topics:
        assert f"{topic}[tiab]" in query or f'"{topic}"[tiab]' in query

    # 日期组是 OR，日期组与范围组之间是 AND
    assert " AND " in query
    assert query.count("[edat]") == 2
    assert query.count("[jour]") == len(cfg.journals)


def test_scope_group_is_or_between_journals_and_topics(cfg):
    """期刊与主题词之间必须是 OR。

    用 AND 会把纯 EP 期刊里的影像/基础研究全滤掉（它们标题里没有主题词），
    白名单的意义就没了。
    """
    query = build_query(["Heart Rhythm"], ["atrial fibrillation"], [TODAY])
    assert query == (
        '(("Heart Rhythm"[jour]) OR ("atrial fibrillation"[tiab]))'
        ' AND ("2026/10/19"[edat])'
    )
    assert "Heart Rhythm\"[jour] AND" not in query


def test_date_field_is_parameterised_not_hardcoded():
    """date_field 可以切换（排查用），此时不应再出现 [edat]。"""
    query = build_query(["Heart Rhythm"], [], [TODAY], date_field="pdat")
    assert '"2026/10/19"[pdat]' in query
    assert "[edat]" not in query


# ---------------------------------------------------------------------------
# 语法细节
# ---------------------------------------------------------------------------

def test_build_clause_single_and_multi():
    assert build_clause([], "jour") == ""
    assert build_clause(["Europace"], "jour") == "(Europace[jour])"
    assert build_clause(["Heart Rhythm"], "jour") == '("Heart Rhythm"[jour])'
    multi = build_clause(["Heart Rhythm", "Europace"], "jour")
    assert multi == '("Heart Rhythm"[jour] OR Europace[jour])'


def test_build_clause_skips_blank_terms():
    assert build_clause(["", "   ", "Europace"], "jour") == "(Europace[jour])"


def test_build_date_clause_quotes_slashes():
    clause = build_date_clause([date(2026, 1, 5)], "edat")
    assert clause == '("2026/01/05"[edat])'


def test_empty_inputs_do_not_crash():
    assert build_query([], [], []) == ""
    # 只有日期时退化成纯日期检索（排查用），不应抛异常
    only_date = build_query([], [], [TODAY])
    assert only_date == '("2026/10/19"[edat])'


# ---------------------------------------------------------------------------
# E-utilities 参数：tool / email / api_key
# ---------------------------------------------------------------------------

def test_esearch_params_always_carry_tool_and_email(cfg):
    params = esearch_params("dummy", cfg)
    assert params["tool"]                     # NCBI 硬性要求
    assert params["email"]                    # NCBI 硬性要求
    assert params["db"] == "pubmed"
    assert params["retmode"] == "json"


def test_api_key_included_only_when_present(cfg):
    cfg.ncbi_api_key = ""
    assert "api_key" not in esearch_params("dummy", cfg)
    cfg.ncbi_api_key = "SECRETKEY"
    assert esearch_params("dummy", cfg)["api_key"] == "SECRETKEY"
    assert efetch_params(["1"], cfg)["api_key"] == "SECRETKEY"


def test_efetch_params_format(cfg):
    params = efetch_params(["1", "2", "3"], cfg)
    assert params["id"] == "1,2,3"
    assert params["retmode"] == "xml"  # 标准库 xml.etree 解析，不用 lxml


def test_placeholder_email_when_unset(cfg):
    cfg.ncbi_email = ""
    assert esearch_params("dummy", cfg)["email"]  # 不能是空串，NCBI 会拒绝


# ---------------------------------------------------------------------------
# Europe PMC 检索式
# ---------------------------------------------------------------------------

def test_europepmc_uses_title_abs_not_bare_words(cfg):
    """必须带 TITLE_ABS: 限定。

    实测：不加字段限定时搜 "cardiac electrophysiology" 会命中 Cell Reports 的
    脑瘤论文 —— 默认是全文/全字段匹配。
    """
    query = europepmc.build_query(cfg.journals, cfg.topics, date_range(2, TODAY))
    assert "TITLE_ABS:" in query
    for topic in cfg.topics:
        assert f"TITLE_ABS:{topic}" in query or f'TITLE_ABS:"{topic}"' in query


def test_europepmc_uses_first_pdate_range(cfg):
    query = europepmc.build_query(["Europace"], ["atrial fibrillation"], date_range(2, TODAY))
    assert "FIRST_PDATE:[2026-10-18 TO 2026-10-19]" in query
    # 不能把 PubMed 的 [edat] 语法抄过来
    assert "[edat]" not in query


def test_europepmc_uses_journal_field(cfg):
    query = europepmc.build_query(["Heart Rhythm"], [], [TODAY])
    assert 'JOURNAL:"Heart Rhythm"' in query


def test_europepmc_search_params(cfg):
    params = europepmc.search_params("dummy")
    assert params["format"] == "json"
    assert params["resultType"] == "core"   # core 才带摘要/作者全名
    assert params["pageSize"] > 0


def test_europepmc_query_without_dates():
    """没有日期窗口时仍要带 SRC 过滤（排预印本），但不产生空日期子句。

    断言的是**不变量**而不是死写整串：查询串形态后续可能微调，
    但「排预印本」「不留空日期子句」这些性质必须一直在。
    """
    query = europepmc.build_query(["Europace"], [], [])
    assert "SRC:" in query
    assert "SRC:PPR" not in query           # 预印本必须被排除
    assert "FIRST_PDATE" not in query       # 没有日期窗口就不该有日期子句
    assert "JOURNAL:Europace" in query
    assert "TITLE_ABS:Europace" in query    # 期刊词同时匹配标题，兜住 PMC 记录


def test_europepmc_query_invariants_with_full_config(cfg):
    """用真实配置检查查询契约的四条不变量。"""
    query = europepmc.build_query(cfg.journals, cfg.topics, date_range(2, TODAY))

    # 1. 服务端就排掉预印本
    assert "SRC:MED" in query and "SRC:PMC" in query and "SRC:AGR" in query
    assert "SRC:PPR" not in query

    # 2. 日期区间格式正确（Europe PMC 用短横线 + TO，闭区间）
    assert "FIRST_PDATE:[2026-10-18 TO 2026-10-19]" in query
    assert "[edat]" not in query            # 不能把 PubMed 语法抄过来

    # 3. 期刊词同时以 JOURNAL: 与 TITLE_ABS: 出现
    for journal in cfg.journals:
        quoted = journal if " " not in journal else f'"{journal}"'
        assert f"JOURNAL:{quoted}" in query
        assert f"TITLE_ABS:{quoted}" in query

    # 4. 主题词都带 TITLE_ABS: 字段限定
    for topic in cfg.topics:
        quoted = topic if " " not in topic else f'"{topic}"'
        assert f"TITLE_ABS:{quoted}" in query


def test_europepmc_date_range_is_closed_interval():
    """闭区间：只有一天时起止相同，不能漏成开区间。"""
    query = europepmc.build_query([], [], [TODAY])
    assert "FIRST_PDATE:[2026-10-19 TO 2026-10-19]" in query


def test_europepmc_journal_also_matched_in_title_abs():
    """只写 JOURNAL: 会漏掉 PMC 记录（其检索索引覆盖较差），需同时匹配标题。"""
    query = europepmc.build_query(["Europace"], [], [])
    assert "TITLE_ABS:Europace" in query
