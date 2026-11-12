export function Overview() {
  return (
    <>
      <section className="page-intro">
        <p className="eyebrow">Portfolio intelligence</p>
        <h1>A clearer perspective.</h1>
        <p className="lede">Your workspace for understanding portfolio performance, positioning and risk.</p>
      </section>
      <section className="overview-placeholder" aria-labelledby="overview-title">
        <div className="placeholder-symbol" aria-hidden="true"><span /><span /><span /></div>
        <span className="eyebrow">Overview</span>
        <h2 id="overview-title">The foundation is in place.</h2>
        <p>Portfolio analytics will appear here as the workspace takes shape.</p>
        <div className="placeholder-note"><span aria-hidden="true">◌</span> Data connection comes next</div>
      </section>
      <footer className="page-footer"><span>Portfolio Risk Engine</span><span>Clarity before complexity.</span></footer>
    </>
  );
}
