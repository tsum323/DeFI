import Plotly from "plotly.js-dist-min";
import createPlotlyComponent from "react-plotly.js/factory";
import type { Data, Layout } from "plotly.js";

const Plot = createPlotlyComponent(Plotly);

const BASE: Partial<Layout> = {
  paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
  font: { color: "#c9d1e3", family: "Inter, system-ui, sans-serif", size: 12 },
  margin: { l: 56, r: 16, t: 24, b: 40 },
  xaxis: { gridcolor: "#1d2638", zerolinecolor: "#2a3550" },
  yaxis: { gridcolor: "#1d2638", zerolinecolor: "#2a3550" },
  legend: { orientation: "h", y: -0.2 }, hovermode: "x unified",
};

export function Chart({ data, layout, height = 320 }: { data: Data[]; layout?: Partial<Layout>; height?: number }) {
  return (
    <Plot data={data} layout={{ ...BASE, ...layout, height, xaxis: { ...BASE.xaxis, ...layout?.xaxis },
      yaxis: { ...BASE.yaxis, ...layout?.yaxis } }}
      config={{ displayModeBar: false, responsive: true }} style={{ width: "100%" }} useResizeHandler />
  );
}
