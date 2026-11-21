# Global Intelligence Radar · 手动基准仓库

此包只包含 GitHub Actions 手动运行需要的文件。完整项目和历史数据库在本地维护。运行环境为公开 GitHub Free 仓库的标准 Ubuntu runner。首次基准使用公开模型验证英文和葡语，模型不可用时记录结果并继续验证原文回退。工作流只在 Actions 页面手动触发。生成的新闻、SQLite 和模型都留在该次 runner 的临时目录。禁止把凭据、历史数据库或其他本地文件加入首轮提交。
