# CANARY: planted misconfigurations. Never built.
FROM node:20-alpine
ENV DB_PASSWORD=canary-not-a-real-password
COPY . .
RUN npm install
CMD ["node", "dist/main.js"]
