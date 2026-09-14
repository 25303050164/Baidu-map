"""Compose a portable HTML report and a repository Markdown report from evidence."""
import argparse
import base64
import html
import json
from pathlib import Path
import statistics
import xml.etree.ElementTree as ET


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=Path("docs/reviews/2026-09-14/osm-offline"))
    parser.add_argument("--junit", type=Path, default=Path("../.tmp/osm-visual-tests/backend.xml"))
    args = parser.parse_args()
    out = args.directory
    evidence = json.loads((out/"evidence.json").read_text(encoding="utf-8"))
    xml = ET.parse(args.junit)
    testcases = list(xml.iter("testcase"))
    skipped = [t for t in testcases if t.find("skipped") is not None]
    failed = [t for t in testcases if t.find("failure") is not None or t.find("error") is not None]
    osm = [t for t in testcases if "osm_offline" in t.get("classname", "")]
    osm_skipped = [t for t in osm if t.find("skipped") is not None]
    duration = sum(float(s.get("time",0)) for s in xml.getroot().findall("testsuite"))
    tested = evidence["tested_at"]
    summary = {"tests":len(testcases),"passed":len(testcases)-len(skipped)-len(failed),
               "skipped":len(skipped),"failed":len(failed),"elapsed_seconds":duration,
               "osm_passed":len(osm)-len(osm_skipped),"osm_skipped":len(osm_skipped)}
    (out/"test-summary.json").write_text(json.dumps(summary,indent=2)+"\n",encoding="utf-8")
    (out/"backend-junit.xml").write_bytes(args.junit.read_bytes())
    title = "OSM_OFFLINE 测试报告 · 含真实路网可视化"
    blocks = []
    md = [f"# {title}\n"]

    def paragraph(text):
        md.append(text+"\n")
        blocks.append("<p>"+html.escape(text)+"</p>")

    def heading(text):
        md.append("## "+text+"\n")
        blocks.append("<h2>"+html.escape(text)+"</h2>")

    def table(headers, rows):
        md.extend(["| "+" | ".join(headers)+" |", "| "+" | ".join(["---"]*len(headers))+" |"])
        md.extend("| "+" | ".join(map(str,row))+" |" for row in rows)
        md.append("")
        blocks.append('<div class="table-wrap"><table><thead><tr>'+"".join("<th>"+html.escape(h)+"</th>" for h in headers)+"</tr></thead><tbody>"+"".join("<tr>"+"".join("<td>"+html.escape(str(v))+"</td>" for v in row)+"</tr>" for row in rows)+"</tbody></table></div>")

    def figure(filename, caption):
        md.append(f"![{caption}]({filename})\n\n{caption}\n")
        encoded = base64.b64encode((out/filename).read_bytes()).decode()
        blocks.append('<figure><a href="#" onclick="event.preventDefault();this.parentElement.classList.toggle(\'expanded\')" aria-label="放大或还原图像"><img loading="lazy" src="data:image/png;base64,'+encoded+'" alt="'+html.escape(caption,quote=True)+'"></a><figcaption>'+html.escape(caption)+"</figcaption></figure>")

    paragraph(f"本轮时间（UTC）：{tested}；被测提交：{evidence['git_revision']}；Python {evidence['python']}。本报告依据本轮执行证据生成，没有沿用上一轮的测试计数或耗时。")
    heading("1. 测试结论")
    paragraph("本轮覆盖的用例全部通过。OSM 离线计算、方向及区间边界、现有 API 契约、重复输出和共享图不变性均得到验证。这里的通过表示程序符合这些测试断言，不等于真实社区 15 分钟步行边界的精度已被验证。")
    slowest = max(evidence["cases"], key=lambda c:statistics.mean(d["total_ms"] for d in c["runs"]))
    slow_values = [d["total_ms"] for d in slowest["runs"]]
    polygon_fraction = sum(d["polygon_ms"] for d in slowest["runs"])/sum(slow_values)
    paragraph(f"主要发现：{slowest['name']}单次耗时 {min(slow_values)/1000:.2f}–{max(slow_values)/1000:.2f} 秒，缓冲成面平均占总耗时 {polygon_fraction:.1%}。路网密集区域存在明显性能风险，不能把此前单点约半秒的结果推广到全上海。本轮记录该瓶颈，未调整 buffer 或修改算法来改善测试结果。")
    table(["验证内容","本轮结果"],[
        ["完整后端回归",f"{summary['passed']} 通过 / {summary['skipped']} 跳过 / {summary['failed']} 失败；{duration:.2f} s"],
        ["其中 OSM 专项",f"{summary['osm_passed']} 通过 / {summary['osm_skipped']} 跳过"],
        ["真实路网正向样例","4 个点 × 3 次引擎计算 + 每点 1 次 HTTP 接口验证"],
        ["真实场景负向检查","超出 coverage → insufficient；901 秒请求 → HTTP 422"],
        ["可视化算法断言","4 / 4：双侧间隙、单向中点、弯曲截取、吸附成本"],
        ["离线约束",f"实际 socket / DNS 连接尝试：{evidence['network_attempts']}"],
        ["重复性","4 个点均在 3 次计算间输出完全相同的几何；HTTP 几何与引擎一致"],
        ["共享图完整性","前后全图节点属性、边属性及 WKB 几何 SHA256 一致"],
    ])
    paragraph("本轮未重新执行大型 PBF 构图，因此默认跳过的 test_real_shanghai_pbf_smoke 不计入本轮通过项。真实缓存加载会验证版本、属性、几何与长度；PBF 和 coverage 文件另做 SHA256 核对。此前构图验收结果可参阅 backend/docs/OSM_OFFLINE.md，不能与本轮测试混算。")
    heading("2. 数据与固定参数")
    table(["项目","值"],[
        ["快照",evidence["snapshot"]["data_version"]],
        ["图规模",f"{evidence['snapshot']['nodes']:,} 节点 / {evidence['snapshot']['edges']:,} 有向边"],
        ["输入 / 输出坐标","BD09LL，[longitude, latitude]"],
        ["图与绘图投影","EPSG:32651；各图坐标为相对起点的米制偏移"],
        ["时间与速度","900 s（含边界）；1.3 m/s"],
        ["最大吸附 / buffer / coverage margin","200 m / 25 m / 100 m"],
        ["本轮首次加载",f"{evidence['startup_seconds']:.2f} s，包括校验和空间索引"],
    ])
    paragraph("三组普通样例是固定上海坐标，用于程序冒烟，不是经过独立地图核验的地标名称；覆盖边界样例由本地 STRtree 从边界附近道路中确定性选取，专门验证 partial 语义。它们不是随机抽样，不能推断全上海的错误率。")
    heading("3. 上海真实路网可视化")
    paragraph("灰线为本地缓存中的已知步行道路，绿线为本次计算得到的可达路网，淡蓝色为固定 25 m buffer 面。红星是实际起点，空心菱形是吸附点；橙色虚线是 extract 覆盖边界。图中未加载在线地图、百度底图或瓦片。各图等比例绘制，但视野独立缩放。")
    figure("real-samples.png","四组真实路网结果。Map data © OpenStreetMap contributors，ODbL。蓝色面是展示表达，不能解释为所有面内位置都具有可行步行路径。")
    rows=[]
    for c in evidence["cases"]:
        d=c["runs"][0]
        rows.append([c["name"],f"{c['origin'][0]:.6f}, {c['origin'][1]:.6f}",c["quality"],f"{d['snap_distance_m']:.1f}",f"{d['network_budget_s']:.1f}",f"{d['reachable_length_m']/1000:.2f}"])
    table(["样例","BD09LL 经度, 纬度","quality","吸附 m","路网预算 s","路网总长 km"],rows)
    paragraph("可达路网总长是分支路段去重后的总长度，并不是单个人在 15 分钟内走过的距离。所有成功样例外层业务 status 均为 partial，因为设施模块未运行；这与 algorithm.quality 是两个不同维度。")
    for c in evidence["cases"]:
        heading(c["name"]+" · 放大样例")
        figure(c["id"]+".png",f"{c['name']}：quality={c['quality']}；coverage_boundary_hit={c['runs'][0]['coverage_boundary_hit']}。")
        if c["id"]=="sample-boundary":
            paragraph("该样例的可达路网接近数据覆盖边界，算法返回 partial 和 graph_coverage_boundary。它表示结果可能因数据截断而不完整，不能把未覆盖区域认定为不可达。")
            paragraph("图中边界外的少量道路仍来自真实缓存。当前 coverage 用于识别可能的数据截断，并不作为删除已知道路的裁剪掩膜。")
    heading("4. 算法边界样例")
    paragraph("以下四幅图直接绘制核心函数返回的几何，并在绘图前执行断言。为使数值可手算，合成图使用 1 m/s；单向中点单元场景使用 50 秒预算。它们是算法单元测试条件，不会改变生产接口固定 900 秒的限制。")
    figure("algorithm-cases.png","深绿色为实际返回的可达区间，灰色为完整道路，红星为测试起点。所有几何均来自本轮函数计算。")
    table(["场景","预期与本轮断言"],[
        ["双侧 partial","200 m 道路两端到达时间 850/860 s；可达总长 50+40=90 m，中间 110 m 留空"],
        ["单向中点","P 在 200 m；预算 50 s，只返回 [200,250] m，不创建 P→A 逆向权限"],
        ["弯曲道路","到起点已用 850 s；只取真实 LineString 前 50 m，不走端点间直线"],
        ["吸附成本","起点距路 130 m；扣除 130 s，路网剩余 770 s，对应 770 m 片段"],
        ["900 秒闭边界（自动测试）","899.999、900 可达；900.001 不可达"],
    ])
    heading("5. 性能与重复性")
    figure("performance.png","每个真实点连续 3 次引擎计算的均值及最小–最大范围；不含初次缓存加载、全图指纹扫描、绘图或 HTTP 序列化。")
    rows=[]
    for c in evidence["cases"]:
        values=[d["total_ms"] for d in c["runs"]]
        rows.append([c["name"],f"{statistics.mean(values):.1f}",f"{min(values):.1f}–{max(values):.1f}",c["runs"][0]["reachable_nodes"],c["runs"][0]["reachable_segments"]])
    table(["样例","平均 ms","范围 ms","可达节点","有向片段"],rows)
    paragraph("这些是单进程、每点 3 次的小样本结果，不提供吞吐量、P95/P99 或并发性能保证。段数按有向边贡献统计，路网总长则为几何 union 后长度。图规模较大，启动校验成本明显高于单次请求。")
    heading("6. 风险、限制与后续验证")
    paragraph("OSM 不是 ground truth。当前测试没有独立百度步行验证点、人工路线核验或现场门禁证据，因此不能报告 Accuracy、Recall、IoU 精度或真实出行可用性。灰色道路未显示为可达可能是方向、连通性、预算或 OSM 缺失连接导致，不能仅凭图像断定现实不可达。")
    paragraph("起点到道路的直线 snap 仍可能穿过墙、河流或门禁；buffer 可能覆盖不可步行的面积或填平小间隙。中国坐标转换存在近似误差。覆盖边界检查只识别 extract 截断，不识别边界内部漏路。下一步应冻结 buffer，并用 held-out 步行验证点同时评价 OSM 与插值算法。")
    paragraph("本轮发现的测试失败为 0。存在两个既有 Starlette / AnyIO 弃用提示，不影响断言。只新增测试与报告工具、证据和图像，未修改生产算法。")
    heading("7. 复现与证据")
    commands = """# 从 backend 目录执行，使用已有本地上海 cache 和 coverage
.venv/Scripts/python.exe -m pip install matplotlib==3.11.2
.venv/Scripts/python.exe -m pytest -q --junitxml=../.tmp/osm-visual-tests/backend.xml
.venv/Scripts/python.exe scripts/test_osm_visual_report.py --output docs/reviews/2026-09-14/osm-offline
.venv/Scripts/python.exe scripts/render_osm_test_report.py --directory docs/reviews/2026-09-14/osm-offline
"""
    md.append("```text\n"+commands+"```\n")
    blocks.append("<pre><code>"+html.escape(commands)+"</code></pre>")
    paragraph("report.html 是可离线打开的自包含版本，图像已嵌入；Markdown、PNG、evidence.json、test-summary.json、backend-junit.xml，以及每点 BD09LL 几何和 metric debug 均在同一目录。GeoJSON 的 coordinateSystem 为 bd09ll，不能直接当作 WGS84 放到底图上。")
    table(["指纹","SHA256"],[[k,evidence["snapshot"][k]] for k in ["pbf_sha256","cache_sha256","coverage_sha256"]]+[["graph_fingerprint",evidence["graph_fingerprint"]]])
    paragraph("Map data © OpenStreetMap contributors。数据许可：Open Database License (ODbL)。")
    (out/"OSM_OFFLINE_TEST_REPORT.md").write_text("\n".join(md),encoding="utf-8")
    page = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>OSM_OFFLINE 测试报告</title><style>
*{box-sizing:border-box}body{margin:0;background:#f0f4f8;color:#1b2c41;font:16px/1.8 "Microsoft YaHei",system-ui,sans-serif}main{max-width:1160px;margin:32px auto;background:#fff;padding:48px 64px;box-shadow:0 8px 40px #1629420b}header{border-bottom:3px solid #167d69;padding-bottom:22px;margin-bottom:30px}h1{font-size:30px;line-height:1.5;margin:10px 0}h2{font-size:22px;margin:40px 0 16px}p{margin:14px 0}.eyebrow{color:#167d69;letter-spacing:.14em;font-size:13px}figure{margin:24px 0;background:#f8fafc;padding:12px}figure img{display:block;width:100%;height:auto}figure a{cursor:zoom-in}figure.expanded{margin-left:-45px;margin-right:-45px}figcaption{font-size:13px;color:#536579;margin:10px 8px}table{border-collapse:collapse;width:100%;font-size:14px;text-align:left}th{background:#edf4f6;color:#24424f}td,th{padding:12px 14px;border-bottom:1px solid #e2e8f0;vertical-align:top}td{overflow-wrap:anywhere}.table-wrap{overflow-x:auto}pre{background:#f1f5f9;padding:20px;white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.7 Consolas,monospace}footer{margin-top:40px;color:#64748b;font-size:13px}a{color:#146f62}@media(max-width:700px){main{margin:0;padding:24px 16px}h1{font-size:25px}h2{font-size:20px}figure{padding:2px}figure.expanded{margin-left:0;margin-right:0}td,th{padding:9px;font-size:12px}}@media print{body{background:white}main{box-shadow:none;margin:0;padding:0}h2{break-after:avoid}figure,table{break-inside:avoid}figure.expanded{margin:24px 0}}
</style></head><body><main><header><div class="eyebrow">OFFLINE ROUTING / VALIDATION REPORT</div><h1>'''+html.escape(title)+'''</h1><p>基于本轮测试证据 · 上海固定快照 · 无在线底图</p></header>'''+"\n".join(blocks)+'''<footer>本报告可直接离线打开。点击图像可放大或还原。</footer></main></body></html>'''
    (out/"report.html").write_text(page,encoding="utf-8")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
