# AMap POI Crawler

一个仅依赖 Python 标准库的高德 Web Service POI 采集工具，适用于中国城市、区县及行政区域。

它解决城市级 POI 采集中最常见的几个问题：

- 网格分页和约 200 条/查询的有效上限；
- 高密网格递归四分，避免静默漏数；
- JSONL、CSV、state 三件套，逐任务落盘和断点续爬；
- 多 Web Service Key 按日额度顺序轮换，从失败的当前请求继续；
- QPS 短时限流原 Key 退避重试，不误切下一把 Key；
- 按 POI ID 去重、按目标行政区 `adcode` 过滤邻市数据；
- 原始 GCJ-02 CSV 与可选 WGS84 CSV，均为 UTF-8-SIG。

> 数据来自高德地图第三方实时数据库，可能存在停业、改名、分类或位置更新延迟，不等同于逐条现场核验。请遵守高德开放平台服务条款、配额与数据使用限制。

## 环境

- Python 3.10+
- 高德开放平台 **Web 服务 API Key**

无需安装第三方 Python 包。

## 快速开始

PowerShell：

```powershell
$env:AMAP_KEYS='key1;key2;key3'
python amap_poi_crawler.py `
  --district-keyword '341000' `
  --types '科教文化:140000,体育场馆:080100' `
  --out-dir './output/huangshan' `
  --output-prefix 'huangshan_poi' `
  --grid-size 0.04 `
  --sleep 0.35 `
  --export-wgs84
```

Bash：

```bash
export AMAP_KEYS='key1;key2;key3'
python amap_poi_crawler.py \
  --district-keyword 341000 \
  --types '科教文化:140000,体育场馆:080100' \
  --out-dir ./output/huangshan \
  --output-prefix huangshan_poi \
  --grid-size 0.04 \
  --sleep 0.35 \
  --export-wgs84
```

密钥只从环境变量读取，不会写入源码、状态文件或结果文件。`AMAP_KEYS` 使用分号分隔，按顺序使用；也兼容单 Key 的 `AMAP_KEY`。

## 先干跑

干跑只查询行政区边界并显示网格数量：

```bash
python amap_poi_crawler.py \
  --district-keyword 341000 \
  --types '科教文化:140000,体育场馆:080100' \
  --dry-run
```

## 输出

运行目录会生成：

| 文件 | 用途 |
| --- | --- |
| `<prefix>.csv` | 清洗后的 GCJ-02 POI 表，UTF-8-SIG |
| `<prefix>_WGS84.csv` | `--export-wgs84` 生成的转换版 |
| `<prefix>_raw.jsonl` | 原始高德 POI，耐久数据源 |
| `<prefix>_state.json` | 待处理任务、完成数、饱和网格 |

使用相同参数和输出目录重新运行，会读取 state，从未完成任务继续。参数变化时请换输出目录，或明确使用 `--no-resume`。

## 关键参数

| 参数 | 说明 |
| --- | --- |
| `--district-keyword` | 中文行政区名、citycode 或 adcode；推荐 adcode |
| `--types` | `名称:类型码`，多类用英文逗号分隔；类型码内部可用 `|` |
| `--grid-size` | 初始网格边长；城市级默认 `0.04`，严格复核可用 `0.02` |
| `--min-grid-size` | 递归拆分下限，默认 `0.00125` |
| `--max-depth` | 最大递归深度，默认 6 |
| `--sleep` | 每次成功请求后的间隔，建议 0.2–0.5 秒 |
| `--max-requests` | 限制本次请求数，便于测试 |
| `--export-wgs84` | 额外输出 WGS84 版本 |

## 如何判断完整

交付前至少检查：

1. `saturated_cells == 0`；
2. POI ID 重复数为 0；
3. 目标行政区外记录为 0；
4. 经纬度缺失为 0；
5. 采集时间为北京时间文本；
6. 对高要求项目再用 `0.02` 网格独立跑一轮，按 POI ID 比较集合差异并取并集。

## 坐标说明

高德 Web Service POI 原始坐标是 **GCJ-02**，不是 BD-09。`--export-wgs84` 使用常见近似反算公式生成 WGS84 版本；工程 GIS 项目应根据精度要求抽样核验。

## 安全与开源协议

- 不要在 issue、截图或提交记录中暴露真实 Key；参考 [SECURITY.md](SECURITY.md)。
- 本项目使用 [MIT License](LICENSE)。

