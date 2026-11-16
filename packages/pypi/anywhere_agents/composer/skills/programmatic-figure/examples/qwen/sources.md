# Qwen4-Max plotting examples

The [Qwen4-Max release article](https://qwen.ai/blog?id=qwen4-max) supplies both datasets. The plots use the lab palette. Coral circles or bars identify Qwen4-Max, and mint diamonds or bars identify Qwen3.8-Max-0902.

[benchmarks.csv](benchmarks.csv) transcribes the printed values from six panels of the [published benchmark grid](https://img.alicdn.com/imgextra/i3/O1CN01d8WOyjWDWEKAxlwO_!!6000000007934-2-tps-5400-3600.png). The selection includes coding, long-horizon work, professional tasks, and bioinformatics. Domain subtitles are abbreviated. These panels share a 0 to 100 display range. Compare models within a benchmark. The empty GPT6.1-Sol cell for Agents' Last Exam corresponds to an omitted bar in the source. The 42.6 bar uses the medium gray of GPT6-Astra in the original legend.

The in-bar Qwen, Claude, OpenAI, and Kimi marks identify model families. The legend gives each model variant. The builder renders the bundled monochrome SVG paths from [LobeHub Icons](https://github.com/lobehub/lobe-icons/tree/55746b0b082258211121c70bd26e6878fd3bd292/packages/static-svg/icons) in lab ink on white. The [upstream license](logos/LICENSE) accompanies the files.

[efficiency.csv](efficiency.csv) records approximate coordinates of the twelve markers in the [published token-efficiency chart](https://img.alicdn.com/imgextra/i1/O1CN010VgwbmS9yHH6DCXk_!!6000000004985-2-tps-3060-1440.png). Each panel was calibrated against its own linear axis ticks. Token counts are rounded to the nearest thousand and scores to one decimal. The upper brackets subtract the two digitized Xhigh token counts for the same benchmark.

Run `python examples/qwen/plot.py --out /path/to/figures` from the skill directory with Python, Matplotlib, and fontTools. It writes PDF, SVG, and PNG files in the chosen output directory.
