# wavtool — 本地 WAV 音频处理工具

一个纯 Python 标准库实现的命令行 WAV 处理工具。**完全本地运行**：所有
音频数据、中间状态和输出只存在于本地文件和内存中，不依赖任何音频服务
器、云 API、媒体服务或网络访问，也不会播放音频或打开 GUI。

## WAV 支持范围

- 容器：RIFF/WAVE（小端）
- 编码：未压缩 PCM（audio format = 1）
- 声道：mono（1）/ stereo（2）
- 位深：16-bit 有符号 PCM
- 严格校验：RIFF 魔数与长度、`fmt ` chunk 各字段一致性
  （byte rate、block align）、`data` chunk 长度必须是 block align 的
  整数倍且不越出文件末尾；损坏或格式不符的文件会以明确的错误信息拒绝。

## 功能与 DSP 算法

处理链路（全部在 float64 域中进行，避免整数溢出）：

    读取分块 → 裁剪 → 声道混合 → FIR 低通 → 增益 → 淡入淡出
            → 重采样 → 饱和截断（saturation）→ 写出

- **音量调整**：`gain = 10^(dB/20)` 线性增益。
- **声道混合**：stereo → mono 为 `0.5*(L+R)`；mono → stereo 为复制。
- **裁剪**：保留 `[start, end)` 秒区间，按帧边界对齐。
- **淡入淡出**：线性斜坡，长度可独立配置。
- **重采样**：线性插值。输出长度严格为
  `round(n_in * rate_out / rate_in)`；末端位置钳制在最后一帧，
  不会越界读取。
- **FIR 低通**：既可直接给定 coefficients，也可用 `--lowpass CUTOFF`
  自动生成 Hamming 窗 sinc 系数（`--taps` 控制阶数，直流增益归一化为
  1）。滤波为因果卷积，输出长度等于输入长度。
- **溢出与饱和**：内部一律 float64 处理；写出 int16 时显式四舍五入并
  钳制到 `[-32768, 32767]`，不会发生回绕（wrap-around）。
- **大文件分块处理**：`process_file` 以 block（默认 65536 帧，可用
  `--block-frames` 调整）为单位流式处理，内存中只保留一个块、FIR 尾
  部和重采样器进位，不会同时保存输入/输出的多个完整副本。输出 WAV
  头部长度在写完后回填。

## 使用

    # 增益 -3 dB、转单声道、裁剪 1.0~2.5s、淡入淡出、低通 3 kHz、重采样到 22050 Hz
    python3 -m wavtool in.wav out.wav --gain-db -3 --to-mono \
        --trim 1.0 2.5 --fade-in 0.1 --fade-out 0.2 \
        --lowpass 3000 --resample 22050

    python3 -m wavtool --help

程序结束时在终端打印数值指标（输入/输出帧数与采样率、峰值、饱和样本
数）。也可以作为库使用：

    from wavtool import Pipeline, process_file
    stats = process_file("in.wav", "out.wav",
                         Pipeline(gain_db=-6.0, out_rate=16000))

## 测试

    python3 -m unittest discover -s tests -v

测试全部通过代码生成正弦波、脉冲和静音信号（不落盘任何外部素材），
覆盖：header 解析与各类损坏文件拒绝、增益精度、立体声混合、裁剪与淡
入淡出边界、重采样输出长度与频率保持、FIR 低通通带/阻带指标、clipping
饱和行为，以及流式分块处理与离线处理结果的一致性。所有断言都会把数值
指标（RMS、dB、频率、最大误差等）打印到终端，不播放音频、不打开 GUI。
