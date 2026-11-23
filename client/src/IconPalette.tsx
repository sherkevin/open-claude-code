// A hard colour break gives one-stroke symbols (e.g. a chevron) an orange tip.
export default function IconPalette() {
  return (
    <svg aria-hidden="true" focusable="false" width="0" height="0" className="flint-icon-palette">
      <defs>
        <linearGradient id="flint-icon-edge" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="24" y2="0">
          <stop offset=".62" stopColor="#141619"/><stop offset=".62" stopColor="#f36b13"/>
        </linearGradient>
        <linearGradient id="flint-icon-edge-inverse" href="#flint-icon-edge">
          <stop offset=".62" stopColor="#fff"/><stop offset=".62" stopColor="#ffb076"/>
        </linearGradient>
      </defs>
    </svg>
  );
}
