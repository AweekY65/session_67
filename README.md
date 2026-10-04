# wavtool — 本地 WAV 音频处理工具

纯 Python（仅标准库）实现的离线 WAV 处理工具。**所有音频、中间状态和输出只存在于本地文件或内存中**，不依赖任何音频服务器、云 API、媒体服务或外部服务；测试与验证全部在终端输出数值指标，不播放音频、不打开 GUI。

## WAV 支持范围

- 容器：RIFF/WAVE，严格校验 header（RIFF/WAVE id、chunk 尺寸与文件实际大小、fmt 字段一致性、data 长度对齐等），损坏文件会被明确拒绝。
- 格式：整型 PCM（audio_format = 1），8/16/24/32-bit；mono/stereo（最多 8 声道）。
- 读取：按块流式解码为 `[-1.0, 1.0]` 浮点采样；写入：默认 16-bit PCM，header 尺寸在关闭时回填。
- 不支持 IEEE float、ADPCM、mp3-in-wav 等非整型 PCM 编码（会报 `WavFormatError`）。

## DSP 算法

所有中间计算使用 float64，避免整数溢出；只有量化回整型 PCM 时执行显式 clipping/saturation（`[-1.0, 1.0]` 截断，`-1.0` 映射到最负码值，如 16-bit 的 -32768）。

- **增益**：线性/dB 恒定增益。
- **声道混合**：多声道平均下混 mono；mono 复制为 stereo。
- **裁剪**：按秒指定 `[start, end)` 区间，按帧精确截取。
- **淡入淡出**：线性斜坡，帧索引驱动，分块边界无接缝。
- **重采样**：线性插值 SRC，输出长度严格为 `round(N_in * dst_rate / src_rate)`，末端采样钳位到最后一个输入采样，长度与分块大小无关。
- **FIR 低通**：Hamming 窗 sinc 设计（`--lowpass` + `--taps`），直流增益归一化为 1；也支持直接给定系数（`--coeffs`）。流式实现，每声道维护延迟线状态。

## 大文件分块处理

处理管线 `reader -> stages -> writer` 以固定块（默认 4096 帧）流式运行，任意时刻内存中只有一块采样加少量滤波器/重采样状态，不会同时保存多个完整副本。测试验证了块大小 4096 与 64 的输出逐字节一致。

## 使用

```bash
python3 -m wavtool in.wav out.wav \
    --trim 0.1 0.9 --fade-in 0.05 --fade-out 0.05 \
    --gain-db -3 --to-mono \
    --lowpass 1500 --taps 129 \
    --resample 22050 --bits 16 --block-frames 4096
```

处理顺序固定为：trim → fade → gain → mix → FIR → resample。运行结束在终端打印输入/输出格式、帧数、peak、RMS 等数值指标。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试通过代码生成正弦波、脉冲和静音 WAV，覆盖：

- header 解析与字段校验；7 类损坏文件（错误 RIFF/WAVE id、截断、data 长度不对齐、block_align 错误、非 PCM 格式、非法位深）的拒绝；
- 增益（RMS 比值 ≈ 1.9953 @ +6 dB）；
- 立体声混合（反相立体声下混 mono ≈ 0；mono→stereo 两声道完全一致）；
- 裁剪帧数精确性；淡入淡出增益曲线采样点；
- 重采样输出帧数与频率（过零法估计 440 Hz，上/下采样）；
- FIR 低通（Goertzel 测通带保持、阻带衰减 > 40 dB）、自定义系数的脉冲响应；
- clipping/saturation（+12 dB 后 max=32767 / min=-32768，无符号回绕）；
- 分块与整体处理输出逐字节一致；静音输入输出保持为零。
