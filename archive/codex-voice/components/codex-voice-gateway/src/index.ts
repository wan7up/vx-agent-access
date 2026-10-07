import { loadConfig } from "./config.js";
import { VoiceGateway } from "./gateway.js";

const gateway = new VoiceGateway(loadConfig());
await gateway.listen();

const shutdown = async () => {
  await gateway.close();
  process.exit(0);
};
process.once("SIGINT", shutdown);
process.once("SIGTERM", shutdown);
