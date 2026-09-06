import { useEffect, useRef, useState } from 'react'

// A HUD-readout touch: numbers materialize by counting up rather than just
// appearing, like a telemetry display locking onto a value.
export default function CountUp({ value, format, duration = 700 }) {
  const [display, setDisplay] = useState(0)
  const startRef = useRef(null)
  const fromRef = useRef(0)

  useEffect(() => {
    fromRef.current = display
    startRef.current = null
    let frame

    function tick(timestamp) {
      if (startRef.current === null) startRef.current = timestamp
      const elapsed = timestamp - startRef.current
      const progress = Math.min(1, elapsed / duration)
      const eased = 1 - Math.pow(1 - progress, 3) // ease-out cubic
      setDisplay(fromRef.current + (value - fromRef.current) * eased)
      if (progress < 1) frame = requestAnimationFrame(tick)
    }

    frame = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(frame)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, duration])

  return <>{format(display)}</>
}
