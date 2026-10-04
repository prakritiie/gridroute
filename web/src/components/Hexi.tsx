import { useId } from "react";

export type Pose = "duty" | "key" | "sleep" | "alarm";

/**
 * Hexi — the night dispatcher. A pointy-top H3 cell in cab yellow, checker livery, radio headset.
 * duty: on the radio · key: holds the one key per driver (claim_driver.lua) · sleep: no gateway · alarm: invariant broke
 */
export function Hexi({ pose = "duty", size = 96, className = "" }: { pose?: Pose; size?: number; className?: string }) {
  const id = useId().replace(/:/g, "");
  const hexPts = "60,22 95.5,42.5 95.5,85.5 60,106 24.5,85.5 24.5,42.5";
  const ink = "var(--ink-0)";
  return (
    <svg
      viewBox="0 0 132 124"
      width={size}
      height={(size * 124) / 132}
      className={`hexi hexi--${pose} ${className}`}
      role="img"
      aria-label={`Hexi the dispatcher, ${pose === "sleep" ? "asleep" : pose === "alarm" ? "alarmed" : pose === "key" ? "holding the key" : "on duty"}`}
    >
      <defs>
        <clipPath id={`body-${id}`}>
          <polygon points={hexPts} />
        </clipPath>
        <pattern id={`check-${id}`} width="10" height="10" patternUnits="userSpaceOnUse">
          <rect width="10" height="10" fill="var(--cab)" />
          <rect width="5" height="5" fill={ink} />
          <rect x="5" y="5" width="5" height="5" fill={ink} />
        </pattern>
      </defs>

      <g className="hexi__bob">
        {/* body */}
        <polygon points={hexPts} fill="var(--cab)" />
        <rect x="20" y="78" width="80" height="10" fill={`url(#check-${id})`} clipPath={`url(#body-${id})`} />
        <polygon points={hexPts} fill="none" stroke={ink} strokeWidth="3.5" strokeLinejoin="round" />

        {/* face */}
        {pose === "sleep" ? (
          <>
            <path d="M43 58 q5 4 10 0 M67 58 q5 4 10 0" stroke={ink} strokeWidth="3" fill="none" strokeLinecap="round" />
            <path d="M55 70 q5 2 10 0" stroke={ink} strokeWidth="2.5" fill="none" strokeLinecap="round" />
          </>
        ) : (
          <>
            <circle cx="48" cy="57" r={pose === "alarm" ? 6.5 : 5.2} fill={ink} />
            <circle cx="72" cy="57" r={pose === "alarm" ? 6.5 : 5.2} fill={ink} />
            <circle cx="49.8" cy="55" r="1.8" fill="var(--cab-hi)" />
            <circle cx="73.8" cy="55" r="1.8" fill="var(--cab-hi)" />
            {pose === "alarm" ? (
              <ellipse cx="60" cy="71" rx="4" ry="4.5" fill={ink} />
            ) : (
              <path d="M52 67 q8 7 16 0" stroke={ink} strokeWidth="3" fill="none" strokeLinecap="round" />
            )}
          </>
        )}

        {/* headset */}
        <path d="M27 50 A35 35 0 0 1 93 50" stroke={ink} strokeWidth="4.5" fill="none" strokeLinecap="round" />
        <rect x="18" y="45" width="12" height="19" rx="4" fill={ink} />
        <rect x="90" y="45" width="12" height="19" rx="4" fill={ink} />
        {pose !== "sleep" && (
          <>
            <path d="M96 63 q0 13 -16 13" stroke={ink} strokeWidth="3" fill="none" strokeLinecap="round" />
            <circle className="hexi__mic" cx="78" cy="76" r="3.6" fill="var(--signal)" stroke={ink} strokeWidth="2" />
          </>
        )}
      </g>

      {pose === "key" && (
        // Position on the outer group, animation on the inner one: a CSS transform would replace the attribute
        <g transform="translate(98 74) rotate(-28)">
          <g className="hexi__key">
            <circle cx="0" cy="0" r="9" fill="none" stroke="var(--brass)" strokeWidth="4" />
            <path d="M9 0 h22 M24 0 v7 M30 0 v5" stroke="var(--brass)" strokeWidth="4" strokeLinecap="round" />
          </g>
        </g>
      )}
      {pose === "sleep" && (
        <g className="hexi__z" fill="var(--ink-3)" fontFamily="var(--font-mono)" fontWeight="600">
          <text x="100" y="30" fontSize="13">z</text>
          <text x="110" y="18" fontSize="10">z</text>
        </g>
      )}
      {pose === "alarm" && (
        <path className="hexi__drop" d="M100 30 q6 9 0 13 q-6 -4 0 -13z" fill="#86b6ef" stroke={ink} strokeWidth="1.5" />
      )}
    </svg>
  );
}
