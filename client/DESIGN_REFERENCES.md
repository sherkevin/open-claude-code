# 知行工作台界面依据

2026-11-05。此版修改现有客户端的首页、会话页及共用导航；沿用现有服务、权限、会话与引用。

## 来源及适配

- 品牌色：2026-10-09 Better AI 官网已选 A 方向的 `assets/brand-tokens.json`。近白 `#F8F8F5`、深墨 `#10182C`、紫 `#720B7A`、蓝 `#2155FF`、辅助文字 `#667083`、面板 `#EEF0F2`。这些是该方案参数，不冒称完整官方 VI；hover/selected 的淡紫由品牌紫与白色混合得到。
- SAP Horizon：[字体规则](https://www.sap.com/design-system/fiori-design-web/v1-148/foundations/visual/typography/typography-horizon)、[密度规则](https://www.sap.com/design-system/fiori-design-web/v1-151/foundations/visual/cozy-compact)、[页面类型](https://www.sap.com/design-system/fiori-design-web/v1-120/page-types/floorplans/when-to-use-which-floorplan)。以 16px 为 rem 基准换算：正文/控件 14px，辅助 12px，回答 16px，页面标题 24px、会话标题20px，分区16px，按钮圆角8px、输入4px；屏幕较小时减少间距，保留可读字号。
- 原始主题文件：[SAP theming-base-content / Horizon](https://github.com/SAP/theming-base-content/blob/b8dc6f16557e5b4b3dc4d07e763c8b46178ab9fe/content/Base/baseLib/sap_horizon/css_variables.css)。边线 `#e5e5e5/#d9d9d9`、成功 `#256f3a`、错误 `#aa0808`、警示文字 `#b44f00` 直接取自该文件。品牌紫表达操作和选中，不代替业务成功/错误语义。
- 72 Product 字体：[Monotype 官方设计案例](https://www.monotype.com/resources/case-studies/creating-the-typeface-for-sap-fiori)，设计来源为 SAP 团队与 Terrance Weinzierl；[SAP 官方下载及 Apache 2.0 说明](https://www.sap.com/design-system/fiori-design-web/v1-148/resources/downloads)。字库、许可和固定版本的 SHA-256 位于 `public/fonts/`。中文使用 Noto Sans SC、PingFang SC、Microsoft YaHei 回退；不声称 72 包含中文，不使用 SAP 商标。
- 功能性动效：[IBM Carbon 原始 motion token](https://github.com/carbon-design-system/carbon/blob/593de4deda2d96f47f71cdfc9b30d07bd1302304/packages/motion/tokens/motion.tokens.json)，110/150/240ms，productive 曲线。此处将其适配到 hover、消息淡入和执行记录侧栏，并遵循减少动态效果设置。
- 工业场景补充：[Siemens Industrial Experience](https://ix.siemens.io/docs/home/overview)。用于核对工业工作流的信息组织，不混用 Siemens 品牌色、字库或商标。

## 本产品的决定

首页展示实际可用助手、已配置资料数量和最近会话。所有数据来自原有接口，不增加虚构生产 KPI、MES 或 APS 功能。会话里优先显示业务结果和来源；模型设置通过显式入口展开，权限、不可切换模型、审批与停止逻辑保持原样。文案使用已有业务名称，不加入“赋能”“闭环”“每一步都有记录”等口号。

参考系统提供设计依据，本版适配由项目实施完成；未声称获得外部网站完整工程或由 SAP/Monotype 设计师审定。
