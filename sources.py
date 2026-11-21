"""公开来源及其覆盖范围。"""
from news_web import SOURCES

# 名称、栏目、入口、地区、语言、角色、覆盖。
EXTRA = (
    ("少数派", "全球科技商业", "https://sspai.com/feed", "中国", "zh", "媒体", "新应用、消费电子、数字产品"),
    ("MIT Technology Review", "全球科技商业", "https://www.technologyreview.com/feed/", "美国、全球", "en", "媒体", "新技术、AI、产业研究"),
    ("Lobsters", "全球科技商业", "https://lobste.rs/rss", "英语社区", "en", "社区", "开发工具、系统、开源技术"),
    ("Product Hunt", "全球科技商业", "https://www.producthunt.com/feed", "全球", "en", "产品目录", "产品自报、软件、硬件应用"),
    ("Apple Newsroom", "全球科技商业", "https://www.apple.com/newsroom/rss-feed.rss", "美国、全球", "en", "官方", "消费电子、芯片、平台发布"),
    ("IT之家", "全球科技商业", "https://www.ithome.com/rss/", "中国", "zh", "媒体", "消费电子、汽车、AI、芯片"),
    ("France 24 Español", "全球重大事件", "https://www.france24.com/es/rss", "欧洲、拉美", "es", "媒体", "国际政治、经济、公共安全"),
    ("TechCrunch", "全球科技商业", "https://techcrunch.com/feed/", "美国", "en", "媒体", "创业、融资、AI、产品"),
    ("Ars Technica", "全球科技商业", "https://feeds.arstechnica.com/arstechnica/index", "美国、全球", "en", "媒体", "芯片、云、AI、科技政策"),
    ("The Verge", "全球科技商业", "https://www.theverge.com/rss/index.xml", "美国", "en", "媒体", "消费电子、科技公司、AI"),
    ("IEEE Spectrum Robotics", "全球科技商业", "https://spectrum.ieee.org/feeds/topic/robotics.rss", "全球", "en", "媒体", "机器人、自动化"),
    ("Electrek", "全球科技商业", "https://electrek.co/feed/", "北美、全球", "en", "媒体", "电动汽车、储能、供应链"),
    ("NVIDIA Blog", "全球科技商业", "https://blogs.nvidia.com/feed/", "美国、全球", "en", "官方", "AI芯片、机器人、算力"),
    ("AWS News Blog", "全球科技商业", "https://aws.amazon.com/blogs/aws/feed/", "美国、全球", "en", "官方", "云计算、AI基础设施"),
    ("USGS", "全球重大事件", "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_week.atom", "全球", "en", "官方", "地震"),
)
BASE = (
    ("全球", "en", "机构", "国际事务、人道"),
    ("巴西", "pt", "媒体", "国际及国内新闻"),
    ("全球", "en", "机构", "灾害"),
    ("中国、全球", "en", "官方", "通信、企业、战略、产业"),
    ("美国、全球", "en", "官方", "AI模型和产品"),
    ("美国、全球", "en", "官方", "AI模型和应用"),
)
REGISTRY = tuple(source + metadata for source, metadata in zip(SOURCES, BASE)) + EXTRA
