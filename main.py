from pathlib import Path
from datetime import date
import csv
from io import StringIO

from fastapi import FastAPI, Request, UploadFile, File, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, Integer, String, Date, ForeignKey, func
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, Session

# ----- Paths & templates -----
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ----- Database setup -----
DATABASE_URL = "sqlite:///./pinball.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Player(Base):
    __tablename__ = "players"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    results = relationship("Result", back_populates="player")


class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    event_date = Column(Date, index=True)

    # New optional fields for richer event info
    start_time = Column(String, nullable=True)          # e.g. "7:00 PM"
    location = Column(String, nullable=True)            # e.g. "Free Play Richardson"
    format = Column(String, nullable=True)              # e.g. "3-strike, Match Play"
    registration_url = Column(String, nullable=True)    # e.g. "https://..."
    description = Column(String, nullable=True)         # free text

    results = relationship("Result", back_populates="event")

class Result(Base):
    __tablename__ = "results"
    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"))
    player_id = Column(Integer, ForeignKey("players.id"))
    position = Column(Integer)
    points = Column(Integer)

    event = relationship("Event", back_populates="results")
    player = relationship("Player", back_populates="results")


Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# F1-like points
POINTS_TABLE = {
    1: 25,
    2: 18,
    3: 15,
    4: 12,
    5: 10,
    6: 8,
    7: 6,
    8: 4,
    9: 2,
    10: 1,
}

app = FastAPI(debug=True)


# ----- Simple health-check -----
@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"


# ----- Standings page -----
@app.get("/", response_class=HTMLResponse)
def read_standings(request: Request, db: Session = Depends(get_db)):
    # Sum points per player
    q = (
        db.query(Player.name, func.sum(Result.points).label("total_points"))
        .join(Result)
        .group_by(Player.id)
        .order_by(func.sum(Result.points).desc(), Player.name.asc())
    )

    standings = q.all()

    return templates.TemplateResponse(
        "standings.html",
        {
            "request": request,
            "standings": standings,
        },
    )


# ----- Events page -----
from datetime import date  # make sure this is imported at top

@app.get("/events", response_class=HTMLResponse)
def read_events(request: Request, db: Session = Depends(get_db)):
    today = date.today()

    # Upcoming: today and future
    upcoming_events = (
        db.query(Event)
        .filter(Event.event_date >= today)
        .order_by(Event.event_date.asc())
        .all()
    )

    # Past: before today
    past_events = (
        db.query(Event)
        .filter(Event.event_date < today)
        .order_by(Event.event_date.desc())
        .all()
    )

    return templates.TemplateResponse(
        "events.html",
        {
            "request": request,
            "upcoming_events": upcoming_events,
            "past_events": past_events,
        },
    )

# ----- events detail -----
from fastapi import HTTPException  # add this import near the top

@app.get("/events/{event_id}", response_class=HTMLResponse)
def event_detail(request: Request, event_id: int, db: Session = Depends(get_db)):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    # Get results sorted by finishing position
    results = (
        db.query(Result)
        .join(Player)
        .filter(Result.event_id == event_id)
        .order_by(Result.position.asc())
        .all()
    )

    return templates.TemplateResponse(
        "event_detail.html",
        {
            "request": request,
            "event": event,
            "results": results,
        },
    )

# ----- Upload form -----
@app.get("/admin/upload", response_class=HTMLResponse)
def upload_form(request: Request):
    return templates.TemplateResponse("upload.html", {"request": request})

# ----- event upload -----
@app.get("/admin/events/new", response_class=HTMLResponse)
def new_event_form(request: Request):
    return templates.TemplateResponse("event_new.html", {"request": request})


@app.post("/admin/events/new")
def create_event(
    request: Request,
    name: str = Form(...),
    event_date: str = Form(...),      # YYYY-MM-DD
    start_time: str = Form(None),
    location: str = Form(None),
    format: str = Form(None),
    registration_url: str = Form(None),
    description: str = Form(None),
    db: Session = Depends(get_db),
):
    y, m, d = [int(x) for x in event_date.split("-")]
    ev_date = date(y, m, d)

    event = Event(
        name=name,
        event_date=ev_date,
        start_time=start_time or None,
        location=location or None,
        format=format or None,
        registration_url=registration_url or None,
        description=description or None,
    )

    db.add(event)
    db.commit()
    db.refresh(event)

    return RedirectResponse(url=f"/events/{event.id}", status_code=303)

# ----- Handle CSV upload -----
@app.post("/admin/upload")
async def upload_results(
    request: Request,
    event_name: str = Form(...),
    event_date: str = Form(...),  # YYYY-MM-DD
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):

# Parse event date
y, m, d = [int(x) for x in event_date.split("-")]
ev_date = date(y, m, d)

# Try to find an existing event with same name + date
event = (
    db.query(Event)
    .filter(Event.name == event_name, Event.event_date == ev_date)
    .first()
)

# If not found, create it
if not event:
    event = Event(name=event_name, event_date=ev_date)
    db.add(event)
    db.commit()
    db.refresh(event)


    # Read CSV
    content = await file.read()
    s = content.decode("utf-8")
    f = StringIO(s)
    reader = csv.DictReader(f)

    for row in reader:
        player_name = row["player"].strip()
        position = int(row["position"])

        # Find or create player
        player = db.query(Player).filter_by(name=player_name).first()
        if not player:
            player = Player(name=player_name)
            db.add(player)
            db.commit()
            db.refresh(player)

        points = POINTS_TABLE.get(position, 0)

        result = Result(
            event_id=event.id,
            player_id=player.id,
            position=position,
            points=points,
        )
        db.add(result)

    db.commit()

    return RedirectResponse(url="/", status_code=303)
