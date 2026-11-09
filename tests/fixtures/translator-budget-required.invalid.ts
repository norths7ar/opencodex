import type { ProviderAdapter } from "../../backend/src/adapters/base";
import type { CursorTransportFactoryInput } from "../../backend/src/adapters/cursor/transport";
import type { OcxParsedRequest } from "../../backend/src/types";

declare const adapter: ProviderAdapter;
declare const parsed: OcxParsedRequest;
declare const response: Response;
declare const emit: Parameters<NonNullable<ProviderAdapter["runTurn"]>>[2];

void adapter.buildRequest(parsed);
void adapter.parseStream(response);
void adapter.parseResponse?.(response);
void adapter.runTurn?.(parsed, emit);
const cursorInput: CursorTransportFactoryInput = {
  provider: { adapter: "cursor", baseUrl: "https://api2.cursor.sh" },
};
void cursorInput;
