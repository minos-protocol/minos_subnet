/**
 * PM2 config: runs start-miner.sh (venv, .env, prerequisites).
 * Usage: bash pm2-miner.sh
 *    or: pm2 start ecosystem.miner.config.js
 */
module.exports = {
  apps: [
    {
      name: "minos-miner",
      script: "./start-miner.sh",
      interpreter: "bash",
      cwd: __dirname,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 30000,
      kill_timeout: 15000,
      log_date_format: "YYYY-MM-DD HH:mm:ss Z",
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
