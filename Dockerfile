FROM alpacamarkets/pylivetrader

ARG ALGO
ARG APCA_API_SECRET_KEY
ARG APCA_API_KEY_ID
ARG APCA_API_BASE_URL

ENV ALGO=$ALGO
ENV APCA_API_SECRET_KEY=$APCA_API_SECRET_KEY
ENV APCA_API_KEY_ID=$APCA_API_KEY_ID
ENV APCA_API_BASE_URL=$APCA_API_BASE_URL

RUN mkdir -p /app/{algo,tmp}

ADD algo /app/algo
ADD tmp /app/tmp

WORKDIR /app

CMD pylivetrader run -f algo/$ALGO --statefile tmp/state/$ALGO.pkl
