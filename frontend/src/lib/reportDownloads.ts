import type { ReportArtifact } from '../api/reports';

export function previewJsonDownload(artifact: ReportArtifact) {
  const { report } = artifact, p = report.period;
  return { blob: new Blob([artifact.jsonText], { type: 'application/json' }),
    filename: `portfolio-${report.portfolio_id}-report-${p.requested_start_date ?? 'auto'}-to-${p.requested_end_date ?? 'auto'}-asof-${report.endpoint.valuation_date ?? 'undated'}.json` };
}
export function saveDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  try { link.href = url; link.download = filename; document.body.append(link); link.click(); }
  finally { link.remove(); setTimeout(() => URL.revokeObjectURL(url), 0); }
}
