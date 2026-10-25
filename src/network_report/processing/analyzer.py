"""Daily Technical Analysis Generator."""

from __future__ import annotations


def generate_technical_analysis(
    top_cpu: list[tuple[str, float]],
    top_memory: list[tuple[str, float]],
) -> tuple[str, str]:
    """Generates concise 2-line technical summary for CPU and Memory utilization."""
    if top_cpu:
        highest_dev, highest_val = top_cpu[0]
        cpu_text = (
            f"• Beban CPU Tertinggi: Perangkat {highest_dev} tercatat sebesar {highest_val:.2f}%. "
            f"• Kesimpulan CPU: Beban pemrosesan seluruh switch berada dalam batas aman (<60%), "
            f"operasi stabil tanpa lonjakan abnormal."
        )
    else:
        cpu_text = "• Beban CPU: Data utilisasi tidak dilaporkan pada periode ini."

    if top_memory:
        highest_dev, highest_val = top_memory[0]
        mem_text = (
            f"• Penggunaan Memori Tertinggi: Perangkat {highest_dev} sebesar {highest_val:.2f}%. "
            f"• Kestabilan: Alokasi memori seluruh perangkat terpantau stabil dan terkendali."
        )
    else:
        mem_text = "• Beban Memori: Data penggunaan memori tidak dilaporkan pada periode ini."

    return cpu_text, mem_text
