/**
 * PM2 config: runs start-validator.sh (venv, .env, prerequisites).
 * Usage: bash pm2-validator.sh
 *    or: pm2 start ecosystem.validator.config.js
 */
module.exports = {
  apps: [
    {
      name: "minos-validator",
      script: "./start-validator.sh",
      interpreter: "bash",
      cwd: __dirname,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 30000,
      log_date_format: "YYYY-MM-DD HH:mm:ss Z",
    },
  ],
};
