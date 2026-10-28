FROM node:24-alpine AS build
WORKDIR /web
COPY package*.json ./
RUN npm ci
COPY . ./
ENV ARCLIGHT_STANDALONE=1
RUN npm run build

FROM node:24-alpine AS runner
WORKDIR /web
ENV NODE_ENV=production
ENV PORT=3000
ENV HOSTNAME=0.0.0.0
COPY --from=build --chown=node:node /web/.next/standalone ./
COPY --from=build --chown=node:node /web/.next/static ./.next/static
USER node
EXPOSE 3000
CMD ["node", "server.js"]

FROM nginx:1.28-alpine AS gateway
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
