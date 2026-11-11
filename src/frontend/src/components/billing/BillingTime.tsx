const utcDate = new Intl.DateTimeFormat("en-US", {
  timeZone: "UTC", month: "short", day: "numeric", year: "numeric",
  hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});

export default function BillingTime({ value }: { value: string | null }) {
  if (!value || !Number.isFinite(Date.parse(value))) return <>Unavailable</>;
  return <time dateTime={value} title={value}>{utcDate.format(new Date(value))}</time>;
}
