import { useEffect, useId, useRef, useState } from 'react';
import { chartBounds, chartSegments, dateNumber, observationIndex } from '../lib/performanceCharts';
import type { TimeSeriesLine } from '../lib/performanceCharts';
import { formatValuationDate, formatWeight } from '../lib/format';

interface Props {
  title: string; description: string; lines: TimeSeriesLine[]; unit: 'wealth_index' | 'fraction';
  start: string | null; end: string | null; compact?: boolean;
}
const shortDate = (date: string) => new Intl.DateTimeFormat('en-GB', { month: 'short', year: 'numeric', day: 'numeric', timeZone: 'UTC' }).format(new Date(`${date}T00:00:00Z`));
export function TimeSeriesChart({ title, description, lines, unit, start, end, compact = false }: Props) {
  const id = useId();
  const container = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(800);
  const [selected, setSelected] = useState<number | null>(null);
  useEffect(() => {
    if (!container.current || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(entries => setWidth(Math.max(260, entries[0].contentRect.width)));
    observer.observe(container.current);
    return () => observer.disconnect();
  }, []);
  const drawable = lines.map(line => ({ ...line, segments: line.unavailable.length ? [] : chartSegments(line.points, line.intervals) }));
  const usable = drawable.map(l => ({ ...l, points: l.segments.flat() }));
  const dates = [...new Set(usable.flatMap(l => l.points.map(p => p.date)))].sort();
  const bounds = chartBounds(usable, unit);
  const activeIndex = Math.max(0, Math.min(dates.length - 1, selected ?? dates.length - 1));
  const active = dates[activeIndex];
  const left = 62, right = 20, top = 20, bottom = 40;
  const height = compact ? 200 : 310;
  const first = start && start <= (dates[0] ?? start) ? start : dates[0];
  const last = (end && end >= (dates.at(-1) ?? end) ? end : dates.at(-1)) ?? first;
  const x = (day: string) => left + (dateNumber(day) - dateNumber(first)) / (dateNumber(last) - dateNumber(first) || 1) * (width - left - right);
  const y = (value: number) => top + (unit === 'fraction' ? value - bounds!.minimum : bounds!.maximum - value) /
    (bounds!.maximum - bounds!.minimum) * (height - top - bottom);
  const axisValue = (value: number) => unit === 'fraction' ? formatWeight(value) : value.toFixed(2);
  const tickDates = [...new Set([first, ...(width > 480 && dates.length ? [dates[Math.floor((dates.length - 1) / 2)]] : []), last])].filter(Boolean);
  const valuesAt = (date: string) => usable.map(l => ({ line: l, point: l.points.find(p => p.date === date) }));
  return <section className={`holdings-panel time-series-panel${compact ? ' time-series-compact' : ''}`} aria-labelledby={`${id}-title`}>
    <div className="panel-heading"><h2 id={`${id}-title`}>{title}</h2><span className="panel-caption">{unit === 'wealth_index' ? 'Normalized wealth · baseline 1.00' : 'Loss from running peak · positive magnitude'}</span></div>
    <p id={`${id}-description`} className="panel-caption">{description}</p>
    <ul className="time-series-legend" aria-label={`${title} legend`}>{drawable.map((line, i) => <li key={line.id}><span className={`series-swatch series-${i % 2}`} aria-hidden="true" />{line.label}</li>)}</ul>
    {drawable.map(line => line.unavailable.length > 0 && <div className="chart-unavailable" key={line.id}><strong>{line.label} unavailable</strong><ul>{line.unavailable.map((reason, i) => <li key={i}>{reason}</li>)}</ul></div>)}
    <div ref={container} className="time-series-canvas">
      {bounds && dates.length ? <>
        <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-labelledby={`${id}-title ${id}-description`} onPointerMove={event => {
          const rect = event.currentTarget.getBoundingClientRect();
          const cursor = (event.clientX - rect.left) / rect.width * width;
          const nearest = dates.reduce((best, date, i) => Math.abs(x(date) - cursor) < Math.abs(x(dates[best]) - cursor) ? i : best, 0);
          setSelected(nearest);
        }}>
          <desc>Only actual observations are selectable. Lines connect verified one-session intervals; missing observations are never estimated.</desc>
          {[0, 1, 2, 3, 4].map(i => {
            const value = bounds.minimum + (bounds.maximum - bounds.minimum) * i / 4;
            return <g key={i} className="chart-axis"><line x1={left} x2={width - right} y1={y(value)} y2={y(value)} /><text x={left - 8} y={y(value)} textAnchor="end" dominantBaseline="middle">{axisValue(value)}</text></g>;
          })}
          {tickDates.map((date, i) => <text key={date} x={x(date)} y={height - 12} className="chart-date" textAnchor={i === 0 ? 'start' : i === tickDates.length - 1 ? 'end' : 'middle'}>{shortDate(date)}</text>)}
          {drawable.map((line, i) => <g key={line.id} className={`series-${i % 2}`}>
            {line.segments.map((segment, index) => <g key={index}>
              <path className="series-line" d={segment.map((p, j) => `${j === 0 ? 'M' : 'L'}${x(p.date)},${y(p.value!)}`).join(' ')} />
              {segment.length === 1 && <circle className="series-point" cx={x(segment[0].date)} cy={y(segment[0].value!)} r="3" />}
            </g>)}
          </g>)}
          {active && <g aria-hidden="true"><line className="chart-cursor" x1={x(active)} x2={x(active)} y1={top} y2={height - bottom} />
            {valuesAt(active).map(({ line, point }, i) => point && <circle key={line.id} className={`series-point series-${i % 2}`} cx={x(active)} cy={y(point.value!)} r="4" />)}</g>}
        </svg>
        <div className="chart-observation" id={`${id}-tooltip`} role="status" aria-live="polite" aria-atomic="true">
          <time dateTime={active}>{formatValuationDate(active)}</time>
          {valuesAt(active).map(({ line, point }) => <span key={line.id}><strong>{line.label}: </strong>{point
            ? <>{unit === 'wealth_index' ? point.value!.toString() : formatWeight(point.value === 0 ? 0 : point.value!)}{unit === 'fraction' && <span className="chart-exact-value"> (fraction {point.value!.toString()})</span>}</>
            : 'Unavailable for this date'}</span>)}
        </div>
        <label className="chart-selection" htmlFor={`${id}-observation`}>Select observation <span className="panel-caption">Arrow keys move; Home/End select boundaries</span></label>
        <input id={`${id}-observation`} className="chart-slider" type="range" min="0" max={dates.length - 1} step="1" value={activeIndex}
          aria-valuetext={formatValuationDate(active)} aria-describedby={`${id}-tooltip`} onChange={event => setSelected(Number(event.target.value))}
          onKeyDown={event => { if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) {
            event.preventDefault(); setSelected(observationIndex(event.key, activeIndex, dates.length));
          } }} />
        <details className="chart-data"><summary>View exact observations as a table</summary><div className="holdings-table-scroll" role="region" tabIndex={0} aria-label={`${title} observations`}>
          <table className="holdings-table"><caption className="sr-only">{title}: exact API observations{unit === 'fraction' ? ', drawdown as a decimal fraction' : ', normalized wealth'}</caption><thead><tr><th scope="col">Date</th>{usable.map(l => <th key={l.id} scope="col">{l.label}</th>)}</tr></thead>
            <tbody>{dates.map(date => <tr key={date}><th scope="row">{formatValuationDate(date)}</th>{valuesAt(date).map(({ line, point }) => <td key={line.id}>{point ? point.value!.toString() : 'Unavailable'}</td>)}</tr>)}</tbody></table>
        </div></details>
      </> : <p className="chart-unavailable" role="status">No complete chart series is available for this reporting period.</p>}
    </div>
  </section>;
}
