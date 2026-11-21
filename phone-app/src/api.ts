// The phone never talks to the dashboard: pairing, check-ins and SOS all go through the
// Redis relay (relay.ts). What's left here is shared by both: the device the dashboard
// describes, and the error type the screens show.

export type DeviceInfo = {
  id: string;
  name: string;
  platform: string;
  account: string;
  person: string | null;
};

/** A failure to show, in the relay's or the dashboard's own words where it gave some. */
export class ApiError extends Error {
  readonly status: number; // 0: couldn't reach the relay

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}
