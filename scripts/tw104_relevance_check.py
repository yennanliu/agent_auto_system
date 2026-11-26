"""Live check of the 104 relevance judge (the `task_filter` second gate) against job titles
seen in real tw104_apply runs. Makes ~45 small LLM calls; run it after editing
src/automation/crews/tw104_relevance_crew/config/*.yaml.

    uv run python scripts/tw104_relevance_check.py              # gpt-4o-mini
    uv run python scripts/tw104_relevance_check.py gpt-4o

Exit code 1 when any verdict differs from the expected one.
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.automation.crews.tw104_relevance_crew.crew import TW104RelevanceCrew  # noqa: E402
from src.automation.flows.tw104_apply_flow import _parse_verdict  # noqa: E402
from src.automation.harness.provider import resolve  # noqa: E402

LOOSE = "AI"
LIST = "AI, software, backend, python, cloud, infra"
STRICT = ("只投 AI / 機器學習 / LLM / AI agent 的工程職（工程師、研究員、資料科學家、架構師）。"
          "一般軟體、前端、後端、全端、系統、MIS 職缺不要；韌體、電控、硬體、機構、採購不要；"
          "業務、行銷、客服、設計、影音、資料標註、PM、實習、兼職不要。")
CASES = [  # (filter name, filter, title, expected)
 *[("loose", LOOSE, t, False) for t in [
   "AI 影音剪輯師｜短影音・品牌廣告", "AI課程行銷企劃專員", "AI智能客服訓練專員【桃園市大園區駐點】",
   "AI 語音數據標註專員", "【晶圓事業部】AI產業鏈技術員｜半導體新手友善", "AI Server 專案整合開發採購",
   "AI 動畫特效師", "AI 客戶成功經理 Customer Success Manager", "Mechanical/Thermal Engineer (AI Data Center)",
   "品牌行銷專員（電商社群營運＋AI 內容自動化）", "Senior Business Development Manager – AI Call Agent Solution",
   "AI Artist", "NQ-物控工程師(Material Control Engineer)_AI Server", "AI商務總監(兼職)"]],
 *[("loose", LOOSE, t, True) for t in [
   "AI Agent 後端⼯程師 (Backend Engineer)", "LLM Engineer_FedGPT", "【思邁智能】生成式 AI 工程師 - Gen AI Engineer",
   "AI Data Engineer", "Machine Learning / AI Application Engineer", "AI Computer Vision Engineer / AI圖像⼯程師(智慧零售)",
   "RD21957 AI/ML Software Engineer(Enterprise Networking)", "Data Scientist(Decision Optimization)"]],
 *[("list", LIST, t, True) for t in [
   "Golang 後端工程師", "SRE / Platform Engineer", "全端工程師｜Java / Spring Boot / React", "AWS 雲端工程師",
   "前線部署工程師（Forward Deployed Engineer, FDE）"]],
 *[("list", LIST, t, False) for t in ["業務專員／業務工程師", "財務會計專員", "雲端產品行銷經理", "Python 程式設計講師（兼職）"]],
 *[("strict", STRICT, t, False) for t in [
   "全端工程師", "PHP工程師(內湖)", "MIS資訊工程師", "(中壢工業區)無人機韌體工程師(飛控/動力系統)",
   "【電力電子】電源Platform研發工程師(CSP)", "機器人電控開發工程師", "前端工程師", "AI 科技-SDET Intern (QA team 實習生)",
   "AI Product Planner 產品企劃"]],
 *[("strict", STRICT, t, True) for t in [
   "AI Agent 後端⼯程師 (Backend Engineer)", "LLM Engineer_FedGPT", "AI 應用工程師 (AI Solutions & Integration)",
   "Machine Learning Team Lead", "人工智慧(AI) 與數據科學 副研究員"]],
]


def main() -> int:
    llm, _provider, model = resolve("openai", sys.argv[1] if len(sys.argv) > 1 else "gpt-4o-mini",
                                    temperature=0.2)

    def judge(case):
        name, task_filter, title, want = case
        result = TW104RelevanceCrew(llm=llm).crew().kickoff(inputs={
            "task_filter": task_filter, "job_title": title, "job_meta": ""})
        verdict = _parse_verdict(getattr(result, "raw", str(result)) or "") or {}
        return name, title, want, verdict.get("relevant"), verdict.get("reason", "")

    with ThreadPoolExecutor(8) as pool:
        out = list(pool.map(judge, CASES))
    bad = [o for o in out if bool(o[3]) != o[2]]
    print(f"{model}: {len(out) - len(bad)}/{len(out)} as expected")
    for name, title, want, got, why in bad:
        print(f"  ✗ [{name}] want={want} got={got} | {title[:44]} | {why[:60]}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
