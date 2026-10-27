# 第三方与素材

本仓库不含模型权重、下载缓存、私人日志、账号数据库、API密钥或SSH配置。

- Wan2.1：官方仓库 [Wan-Video/Wan2.1](https://github.com/Wan-Video/Wan2.1)，权重及许可以官方分发为准。
- CogVideoX-2B：[官方模型与Apache-2.0许可证](https://huggingface.co/zai-org/CogVideoX-2b)。2B许可不自动适用于其他尺寸。
- Qwen2.5-1.5B-Instruct：[官方模型](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct)，遵循其原许可证。
- MiniMax H3：当前在作者实验环境运行开放的基础模型，历史示例中也有官方商业API结果，两类来源分开标注。[官方模型](https://huggingface.co/MiniMaxAI/MiniMax-H3)及其[社区许可证](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE)独立于本仓库MIT；使用和分发前需按实际部署范围核对原条款，不能将应用开源理解为模型可无条件商用或公开服务。历史云调用另受供应商条款与账户权限约束。
- H3的固定ComfyUI、量化权重、FP16与双卡社区适配来自独立实验环境；该环境、第三方推理runner与权重没有纳入本CPU审阅源码包。将来分发推理环境时仍须一并核对各自许可证与署名，不能仅附本项目MIT。
- FastAPI、PyTorch、Diffusers、Transformers、ImageIO、Pillow等依赖分别遵守原许可证。
- 字幕字体使用操作者安装的授权字体；仓库未复制系统字体。视频含程序生成音符配乐，不含第三方商业音乐或克隆声音。
- 界面仅参考即梦的功能组织，没有收集其私有接口、下载示例媒体、复制标志/源码或将其生成能力冒充为本站功能。
- `docs/assets/episode-contact.jpg`来自本项目本地Wan原创实验；测试中的移动色块动画是程序合成fixture，不是AI生成成果。

源代码许可证与模型/素材/云服务的许可证相互独立。
