export interface Env {
  TOKENS: KVNamespace;
  TW_PARKING_OPEN?: string;
  TW_PARKING_TOKEN?: string;
  TW_PARKING_HTTP_TTL?: string;
  TW_PARKING_PARKBOSS_URL?: string;
  TDX_CLIENT_ID?: string;
  TDX_CLIENT_SECRET?: string;
  RATE_LIMITER?: {
    limit: (options: { key: string }) => Promise<{ success: boolean }>;
  };
}

export interface ParkingRecord {
  name: string;
  lat: number;
  lon: number;
  car_total?: number | null;
  car_value?: number | null;
  car_time?: string | null;
  pay_info?: string | null;
  source: string;
  geo_precision?: string;
  distance_m?: number;
  fresh_level?: string;
  fresh_text?: string;
  freshness?: string;
  rate_summary?: string;
  navigation_url?: string;
  availability_status?: string;
  [key: string]: any;
}
