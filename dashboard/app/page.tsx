'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  AlertTriangle,
  Atom,
  CheckCircle2,
  ChevronRight,
  CircleGauge,
  Clock3,
  FlaskConical,
  KeyRound,
  LoaderCircle,
  Pencil,
  Play,
  Plus,
  Save,
  Server,
  Square,
  Thermometer,
  Trash2,
  Upload,
  Waves,
  Wifi,
} from 'lucide-react';
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

type Metric = 'energy' | 'electrostatic' | 'temperature' | 'pressure' | 'rmsd';
type Point = {
  step: number;
  time: number;
  energy?: number;
  electrostatic?: number;
  temperature?: number;
  pressure?: number;
  volume?: number;
};
type Stage = {
  file: string;
  stage: string;
  status: 'done' | 'running' | 'waiting' | 'error';
  timestepFs: number;
  seed: number | null;
  errors: string[];
  series: Point[];
};
type AgentDecision = {
  status: 'waiting' | 'continue' | 'ready' | 'error';
  decision: string;
  confidence: string;
  evaluatedStage?: string;
  stabilityReady?: boolean;
  reasons: string[];
  nextAction: string;
  metrics: Record<string, number | null>;
  continuation?: {
    stage: string;
    file: string;
    status: 'created' | 'existing' | 'updated';
    runSteps?: number;
    durationPs?: number;
    timestepFs?: number;
    dcdFrequency?: number;
    dcdIntervalPs?: number;
    atomCount?: number;
    inputStage?: string;
    restrained?: boolean;
  };
};
type DashboardData = {
  source: string;
  generatedAt: string;
  stages: Stage[];
  previousRun?: { stages: Stage[] };
  summary: {
    outputs: number;
    points: number;
    errors: number;
    last: Point | null;
    seed: number | null;
  };
  errors: { stage: string; message: string }[];
  rmsd: {
    status: string;
    reason: string;
    series: {
      stage: string;
      step: number | null;
      value: number;
      source: string;
      capturedAt: string;
    }[];
  };
  rmsf: {
    status: string;
    reason: string;
    series: { residue: string; name: string; segment: string; value: number }[];
  };
  agent: AgentDecision;
};
type SshConnection = {
  id: string;
  name: string;
  host: string;
  port: number;
  username: string;
  authType: 'agent' | 'key' | 'password';
  keyPath: string;
  remoteWorkdir: string;
  setupCommand?: string;
  scheduler: 'none' | 'slurm' | 'pbs';
  status: 'untested' | 'online' | 'error';
  lastTestedAt: string | null;
  lastMessage: string;
  createdAt: string;
  updatedAt: string;
};
type SshForm = Omit<
  SshConnection,
  'id' | 'status' | 'lastTestedAt' | 'lastMessage' | 'createdAt' | 'updatedAt'
> & { id?: string };
declare global {
  interface Document {
    modelContext?: {
      registerTool: (
        tool: unknown,
        options?: { signal?: AbortSignal },
      ) => void | Promise<void>;
    };
  }
}
const metricMeta: Record<
  Metric,
  {
    label: string;
    unit: string;
    color: string;
    domain?: [number, number];
    target?: number;
  }
> = {
  energy: { label: 'Energía potencial', unit: 'kcal/mol', color: '#63e6be' },
  electrostatic: { label: 'ELECT', unit: 'kcal/mol', color: '#42d3ff' },
  temperature: {
    label: 'Temperatura',
    unit: 'K',
    color: '#ffbf69',
    target: 303.15,
  },
  pressure: {
    label: 'Presión media',
    unit: 'bar',
    color: '#74c0fc',
    domain: [0.4, 1.6],
    target: 1.01325,
  },
  rmsd: {
    label: 'RMSD proteína',
    unit: 'Å',
    color: '#d0bfff',
    domain: [0, 2.6],
  },
};
const API_BASE = 'http://127.0.0.1:8765';

function dataRevision(item: DashboardData) {
  return [
    item.source,
    item.summary.points,
    item.summary.errors,
    ...item.stages.flatMap((stage) => [
      stage.stage,
      stage.status,
      stage.timestepFs,
      stage.series.length,
      stage.series.at(-1)?.step ?? -1,
      stage.seed ?? -1,
    ]),
    item.rmsd.series.length,
    item.rmsf.series.length,
    item.agent?.status,
    item.agent?.decision,
    item.agent?.continuation?.stage,
    item.agent?.reasons.join('·'),
  ].join('|');
}

function chartWindow<T>(points: T[], maximum = 150) {
  return points.length > maximum ? points.slice(-maximum) : points;
}

function applyStride<T>(points: T[], stride: number) {
  if (stride <= 1 || points.length < 2) return points;
  const filtered = points.filter((_, index) => index % stride === 0);
  const last = points.at(-1);
  if (last !== undefined && filtered.at(-1) !== last) filtered.push(last);
  return filtered;
}

function MetricTooltip({ active, payload, label, metric }: any) {
  if (!active || !payload?.length) return null;
  const meta = metricMeta[metric as Metric];
  return (
    <div className="chart-tooltip">
      <span>paso {label}</span>
      <strong>
        {Number(payload[0].value).toLocaleString('es-CL', {
          maximumFractionDigits: 2,
        })}{' '}
        {meta.unit}
      </strong>
    </div>
  );
}

const emptySshForm: SshForm = {
  name: '',
  host: '',
  port: 22,
  username: '',
  authType: 'agent',
  keyPath: '',
  setupCommand: 'module load namd/3.0.2',
  remoteWorkdir: '~/md-pipeline',
  scheduler: 'none',
};

const SSH_PASSWORD_SESSION_KEY = 'md-pipeline:ssh-passwords';
const EXECUTION_TARGET_STORAGE_KEY = 'md-pipeline:execution-target';

function readSessionPasswords(): Record<string, string> {
  if (typeof window === 'undefined') return {};
  try {
    const value: unknown = JSON.parse(
      window.sessionStorage.getItem(SSH_PASSWORD_SESSION_KEY) ?? '{}',
    );
    if (!value || typeof value !== 'object') return {};
    return Object.fromEntries(
      Object.entries(value).filter(
        ([, password]) => typeof password === 'string' && password.length > 0,
      ),
    );
  } catch {
    return {};
  }
}

function SshModule() {
  const [connections, setConnections] = useState<SshConnection[]>([]);
  const [form, setForm] = useState<SshForm>(emptySshForm);
  const [showForm, setShowForm] = useState(false);
  const [busy, setBusy] = useState('');
  const [message, setMessage] = useState('');
  const [passwords, setPasswords] = useState<Record<string, string>>(
    readSessionPasswords,
  );
  useEffect(() => {
    window.sessionStorage.setItem(
      SSH_PASSWORD_SESSION_KEY,
      JSON.stringify(passwords),
    );
  }, [passwords]);
  async function loadConnections() {
    try {
      const response = await fetch(`${API_BASE}/ssh-connections`, {
        cache: 'no-store',
      });
      const body = (await response.json()) as {
        connections?: SshConnection[];
        error?: string;
      };
      if (!response.ok) throw new Error(body.error);
      setConnections(body.connections ?? []);
    } catch (error) {
      setMessage(
        error instanceof Error
          ? error.message
          : 'No se pudieron cargar las conexiones.',
      );
    }
  }
  useEffect(() => {
    void loadConnections();
  }, []);
  function edit(connection: SshConnection) {
    setForm({
      id: connection.id,
      name: connection.name,
      host: connection.host,
      port: connection.port,
      username: connection.username,
      authType: connection.authType,
      keyPath: connection.keyPath,
      remoteWorkdir: connection.remoteWorkdir,
      setupCommand: connection.setupCommand ?? '',
      scheduler: connection.scheduler,
    });
    setShowForm(true);
    setMessage('');
  }
  async function saveConnection(event: { preventDefault(): void }) {
    event.preventDefault();
    setBusy('save');
    setMessage('');
    try {
      const response = await fetch(`${API_BASE}/ssh-connections`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(form),
      });
      const body = (await response.json()) as {
        connection?: SshConnection;
        error?: string;
      };
      if (!response.ok) throw new Error(body.error || 'No se pudo guardar.');
      await loadConnections();
      setForm(emptySshForm);
      setShowForm(false);
      setMessage('Conexión guardada.');
    } catch (error) {
      setMessage(
        error instanceof Error ? error.message : 'No se pudo guardar.',
      );
    } finally {
      setBusy('');
    }
  }
  async function testConnection(id: string) {
    setBusy(id);
    setMessage('Probando conexión SSH…');
    try {
      const response = await fetch(`${API_BASE}/ssh-test/${id}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: passwords[id] ?? '' }),
      });
      const body = (await response.json()) as {
        connection?: SshConnection;
        error?: string;
      };
      if (body.connection)
        setConnections((items) =>
          items.map((item) => (item.id === id ? body.connection! : item)),
        );
      setMessage(
        body.connection?.lastMessage || body.error || 'Prueba terminada.',
      );
    } catch {
      setMessage('No se pudo contactar la API local.');
    } finally {
      setBusy('');
    }
  }
  async function deleteConnection(id: string) {
    if (!window.confirm('¿Eliminar esta conexión SSH?')) return;
    setBusy(id);
    try {
      const response = await fetch(`${API_BASE}/ssh-connections/${id}`, {
        method: 'DELETE',
      });
      const body = (await response.json()) as {
        deleted?: string;
        error?: string;
      };
      if (!response.ok) throw new Error(body.error);
      setConnections((items) => items.filter((item) => item.id !== id));
      setPasswords((values) => {
        const { [id]: _removed, ...remaining } = values;
        return remaining;
      });
      setMessage('Conexión eliminada.');
    } catch (error) {
      setMessage(
        error instanceof Error ? error.message : 'No se pudo eliminar.',
      );
    } finally {
      setBusy('');
    }
  }
  return (
    <section className="workspace ssh-workspace">
      <header className="ssh-header">
        <div>
          <p className="eyebrow">INFRAESTRUCTURA</p>
          <h1>Conexiones SSH</h1>
          <p>Servidores disponibles para futuras simulaciones NAMD remotas.</p>
        </div>
        <button
          className="upload-button"
          onClick={() => {
            setForm(emptySshForm);
            setShowForm(true);
            setMessage('');
          }}
        >
          <Plus />
          Nueva conexión
        </button>
      </header>
      {message && (
        <div className="ssh-message">
          <Wifi />
          <span>{message}</span>
        </div>
      )}
      {showForm && (
        <form className="panel ssh-form" onSubmit={saveConnection}>
          <div className="panel-head">
            <div>
              <p className="eyebrow">CONFIGURACIÓN</p>
              <h2>{form.id ? 'Editar servidor' : 'Nuevo servidor'}</h2>
            </div>
            <button
              type="button"
              className="text-button"
              onClick={() => setShowForm(false)}
            >
              Cancelar
            </button>
          </div>
          <div className="ssh-fields">
            <label>
              <span>Nombre</span>
              <input
                required
                maxLength={80}
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="GPU laboratorio"
              />
            </label>
            <label>
              <span>Host o IP</span>
              <input
                required
                value={form.host}
                onChange={(e) => setForm({ ...form, host: e.target.value })}
                placeholder="cluster.ejemplo.cl"
              />
            </label>
            <label>
              <span>Puerto</span>
              <input
                required
                type="number"
                min={1}
                max={65535}
                value={form.port}
                onChange={(e) =>
                  setForm({ ...form, port: Number(e.target.value) })
                }
              />
            </label>
            <label>
              <span>Usuario</span>
              <input
                required
                value={form.username}
                onChange={(e) => setForm({ ...form, username: e.target.value })}
                placeholder="guido"
              />
            </label>
            <label>
              <span>Autenticación</span>
              <select
                value={form.authType}
                onChange={(e) =>
                  setForm({
                    ...form,
                    authType: e.target.value as 'agent' | 'key' | 'password',
                  })
                }
              >
                <option value="agent">ssh-agent</option>
                <option value="key">Archivo de clave</option>
                <option value="password">Contraseña</option>
              </select>
            </label>
            <label>
              <span>Scheduler</span>
              <select
                value={form.scheduler}
                onChange={(e) =>
                  setForm({
                    ...form,
                    scheduler: e.target.value as SshForm['scheduler'],
                  })
                }
              >
                <option value="none">Sin scheduler</option>
                <option value="slurm">SLURM</option>
                <option value="pbs">PBS</option>
              </select>
            </label>
            {form.authType === 'key' && (
              <label className="wide">
                <span>Ruta de clave privada en Windows</span>
                <input
                  required
                  value={form.keyPath}
                  onChange={(e) =>
                    setForm({ ...form, keyPath: e.target.value })
                  }
                  placeholder="C:\Users\guido\.ssh\id_ed25519"
                />
              </label>
            )}
            <label className="wide">
              <span>Comando de inicialización del host</span>
              <input
                required
                value={form.setupCommand ?? ''}
                onChange={(e) =>
                  setForm({ ...form, setupCommand: e.target.value })
                }
                placeholder="Ej.: module load namd/3.0.2"
              />
              <small>
                Se guarda únicamente para este host y se ejecuta antes de
                verificar y lanzar NAMD.
              </small>
            </label>
            <label className="wide">
              <span>Directorio de trabajo remoto</span>
              <input
                required
                value={form.remoteWorkdir}
                onChange={(e) =>
                  setForm({ ...form, remoteWorkdir: e.target.value })
                }
                placeholder="~/md-pipeline"
              />
            </label>
          </div>
          <div className="ssh-form-actions">
            <span>
              <KeyRound />
              La contraseña se conserva solo durante esta pestaña; no se guarda
              en SQLite y solo se usa para autenticar esta conexión SSH.
            </span>
            <button className="run-button" disabled={busy === 'save'}>
              {busy === 'save' ? <LoaderCircle className="spin" /> : <Save />}
              Guardar conexión
            </button>
          </div>
        </form>
      )}
      <div className="ssh-grid">
        {connections.map((connection) => (
          <article className="panel ssh-card" key={connection.id}>
            <div className="ssh-card-head">
              <span className={`server-icon ${connection.status}`}>
                <Server />
              </span>
              <div>
                <h2>{connection.name}</h2>
                <p>
                  {connection.username}@{connection.host}:{connection.port}
                </p>
              </div>
              <span className={`ssh-status ${connection.status}`}>
                {connection.status === 'online'
                  ? 'Conectado'
                  : connection.status === 'error'
                    ? 'Error'
                    : 'Sin probar'}
              </span>
            </div>
            <div className="ssh-details">
              <div>
                <span>Directorio remoto</span>
                <strong>{connection.remoteWorkdir}</strong>
              </div>
              <div>
                <span>Scheduler</span>
                <strong>
                  {connection.scheduler === 'none'
                    ? 'Directo'
                    : connection.scheduler.toUpperCase()}
                </strong>
              </div>
              <div>
                <span>Autenticación</span>
                <strong>
                  {connection.authType === 'agent'
                    ? 'ssh-agent'
                    : connection.authType === 'password'
                      ? 'Contraseña'
                      : 'Clave privada'}
                </strong>
              </div>
              <div>
                <span>Última prueba</span>
                <strong>
                  {connection.lastTestedAt
                    ? new Date(connection.lastTestedAt).toLocaleString('es-CL')
                    : 'Nunca'}
                </strong>
              </div>
            </div>
            <p className="ssh-result">{connection.lastMessage}</p>
            {connection.authType === 'password' &&
              connection.status !== 'online' && (
              <label className="ssh-password">
                <span>Contraseña de {connection.username}</span>
                <input
                  type="password"
                  autoComplete="off"
                  aria-label={`Contraseña de ${connection.name}`}
                  value={passwords[connection.id] ?? ''}
                  onChange={(e) =>
                    setPasswords((values) => ({
                      ...values,
                      [connection.id]: e.target.value,
                    }))
                  }
                />
                <small>
                  Se cifra con tu cuenta de Windows y se reutiliza para copiar,
                  ejecutar y detener.
                </small>
              </label>
            )}
            <div className="ssh-card-actions">
              {(connection.authType !== 'password' ||
                connection.status !== 'online') && (
                <button
                  onClick={() => void testConnection(connection.id)}
                  disabled={
                    busy === connection.id ||
                    (connection.authType === 'password' &&
                      !passwords[connection.id])
                  }
                >
                  {busy === connection.id ? (
                    <LoaderCircle className="spin" />
                  ) : (
                    <Wifi />
                  )}
                  {connection.authType === 'password' ? 'Conectar' : 'Probar'}
                </button>
              )}
              <button
                disabled={busy === connection.id}
                onClick={async () => {
                  setBusy(connection.id);
                  try {
                    const response = await fetch(
                      `${API_BASE}/ssh-environment/${connection.id}`,
                      { method: 'POST' },
                    );
                    const body = (await response.json()) as {
                      message?: string;
                      error?: string;
                    };
                    setMessage(
                      body.message ?? body.error ?? 'No se pudo comprobar',
                    );
                  } catch {
                    setMessage('No se pudo contactar la API');
                  } finally {
                    setBusy('');
                  }
                }}
              >
                Verificar NAMD
              </button>
              <button onClick={() => edit(connection)}>
                <Pencil />
                Editar
              </button>
              <button
                className="danger"
                onClick={() => void deleteConnection(connection.id)}
              >
                <Trash2 />
                Eliminar
              </button>
            </div>
          </article>
        ))}
        {!connections.length && !showForm && (
          <div className="panel ssh-empty">
            <Server />
            <h2>No hay servidores configurados</h2>
            <p>
              Agrega una conexión para preparar la ejecución remota del
              pipeline.
            </p>
            <button className="upload-button" onClick={() => setShowForm(true)}>
              <Plus />
              Agregar servidor
            </button>
          </div>
        )}
      </div>
    </section>
  );
}

type Simulation = {
  id: string;
  name: string;
  namdDir: string;
  filename: string | null;
  createdAt: string | null;
  state?: {
    status: 'pending' | 'running' | 'completed' | 'finished' | 'stopped' | 'error';
    currentStep: string | null;
    completedSteps: string[];
    pendingSteps: string[];
    failedSteps: string[];
    progressPercent: number;
    updatedAt: string | null;
  } | null;
};
type SimulationProject = {
  id: string;
  name: string;
  description: string;
  directory?: string | null;
  simulations: Simulation[];
};
type ExecutionTarget = {
  id: string;
  name: string;
  available: boolean;
  detail: string;
};
function ExecutionModule({
  value,
  onChange,
  disabled,
  onManage,
}: {
  value: string;
  onChange: (id: string) => void;
  disabled: boolean;
  onManage: () => void;
}) {
  const [targets, setTargets] = useState<ExecutionTarget[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  async function refresh() {
    setLoading(true);
    try {
      const response = await fetch(`${API_BASE}/execution-targets`, {
        cache: 'no-store',
      });
      if (!response.ok) throw new Error('No se pudieron cargar los destinos.');
      const body = (await response.json()) as { targets: ExecutionTarget[] };
      setTargets(body.targets);
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Error de conexión');
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    void refresh();
  }, []);
  const selected = targets.find((t) => t.id === value);
  return (
    <article className="panel execution-module">
      <div>
        <p className="eyebrow">EJECUCIÓN</p>
        <h2>Dónde correr la simulación</h2>
        <p>{selected?.detail ?? 'Selecciona un destino disponible.'}</p>
      </div>
      <div className="execution-controls">
        <label htmlFor="execution-target">Destino</label>
        <select
          id="execution-target"
          value={value}
          disabled={disabled || loading}
          onChange={(e) => onChange(e.target.value)}
        >
          {!selected && (
            <option value={value} disabled>
              Destino no disponible
            </option>
          )}
          {targets.map((target) => (
            <option
              key={target.id}
              value={target.id}
              disabled={!target.available}
            >
              {target.name}
              {!target.available ? ' · no disponible' : ''}
            </option>
          ))}
        </select>
        <button
          className="text-button"
          disabled={disabled || loading}
          onClick={() => void refresh()}
        >
          {loading ? 'Actualizando…' : 'Actualizar'}
        </button>
        <button className="text-button" onClick={onManage}>
          Gestionar SSH
        </button>
      </div>
      {error && <p role="alert">{error}</p>}
      {selected && !selected.available && (
        <p role="alert">
          Prueba la conexión en SSH y actualiza los destinos antes de ejecutar.
        </p>
      )}
      {value !== 'local' && (
        <p>
          SSH directo: se comprueban NAMD y csh al iniciar. Los archivos .out se
          sincronizan; las trayectorias y reinicios permanecen en el servidor.
        </p>
      )}
    </article>
  );
}

export default function Home() {
  const [view, setView] = useState<'dashboard' | 'ssh'>('dashboard');
  const [targetId, setTargetId] = useState('local');
  const [projects, setProjects] = useState<SimulationProject[]>([]);
  const [projectId, setProjectId] = useState('');
  const [simulationId, setSimulationId] = useState('');
  const [newProjectName, setNewProjectName] = useState('');
  const [projectDescription, setProjectDescription] = useState('');
  const [editingProject, setEditingProject] = useState<string | null>(null);
  const [projectError, setProjectError] = useState('');
  const [projectBusy, setProjectBusy] = useState(true);
  const selectedProject = projects.find((project) => project.id === projectId);
  const selectedSimulation = selectedProject?.simulations.find(
    (simulation) => simulation.id === simulationId,
  );
  const selectedSource = selectedSimulation?.namdDir;

  const [metric, setMetric] = useState<Metric>('energy');
  const [stride, setStride] = useState(1);
  const [selectedStage, setSelectedStage] = useState('auto');
  const [replica, setReplica] = useState('Réplica 01');
  const [data, setData] = useState<DashboardData | null>(null);
  const [loadError, setLoadError] = useState('');
  const [uploadState, setUploadState] = useState<
    'idle' | 'uploading' | 'success' | 'error'
  >('idle');
  const [uploadMessage, setUploadMessage] = useState('');
  const [uploadLogs, setUploadLogs] = useState<string[]>([]);
  const [runState, setRunState] = useState<'idle' | 'running' | 'stopping'>(
    'idle',
  );
  const [currentJobId, setCurrentJobId] = useState<string | null>(null);
  const runPollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const savedTarget = window.localStorage.getItem(
      EXECUTION_TARGET_STORAGE_KEY,
    );
    if (savedTarget) setTargetId(savedTarget);
  }, []);
  const logClock = () =>
    new Date().toLocaleTimeString('es-CL', { hour12: false });
  async function chooseSimulation(
    nextProjectId: string,
    nextSimulationId: string,
  ) {
    setProjectBusy(true);
    setProjectError('');
    setData(null);
    setSelectedStage('auto');
    setUploadState('idle');
    setUploadLogs([]);
    setProjectId(nextProjectId);
    setSimulationId('');
    setEditingProject(null);
    setNewProjectName('');
    setProjectDescription('');
    try {
      if (nextSimulationId) {
        const response = await fetch(`${API_BASE}/select-simulation`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            projectId: nextProjectId,
            simulationId: nextSimulationId,
          }),
        });
        const body = (await response.json()) as {
          simulation?: Simulation;
          state?: Simulation['state'];
          error?: string;
        };
        if (!response.ok)
          throw new Error(body.error || 'No se pudo abrir la simulación.');
        if (body.state) {
          setProjects((items) =>
            items.map((project) =>
              project.id !== nextProjectId
                ? project
                : {
                    ...project,
                    simulations: project.simulations.map((simulation) =>
                      simulation.id === nextSimulationId
                        ? { ...simulation, state: body.state }
                        : simulation,
                    ),
                  },
            ),
          );
        }
        setSimulationId(nextSimulationId);
      }
    } catch (error) {
      setProjectError(
        error instanceof Error
          ? error.message
          : 'No se pudo abrir la simulación.',
      );
    } finally {
      setProjectBusy(false);
    }
  }
  async function loadProjects(
    preferredProject?: string,
    preferredSimulation?: string,
  ) {
    setProjectBusy(true);
    setProjectError('');
    try {
      const response = await fetch(`${API_BASE}/projects`, {
        cache: 'no-store',
      });
      const body = (await response.json()) as {
        projects: SimulationProject[];
        activeNamdDir?: string;
        error?: string;
      };
      if (!response.ok)
        throw new Error(body.error || 'No se pudieron cargar los proyectos.');
      setProjects(body.projects);
      const project =
        body.projects.find((p) => p.id === preferredProject) ??
        body.projects.find((p) =>
          p.simulations.some((sim) => sim.namdDir === body.activeNamdDir),
        ) ??
        body.projects[0];
      const simulation =
        project?.simulations.find((sim) => sim.id === preferredSimulation) ??
        project?.simulations.find(
          (sim) => sim.namdDir === body.activeNamdDir,
        ) ??
        project?.simulations[0];
      await chooseSimulation(project?.id ?? '', simulation?.id ?? '');
    } catch (error) {
      setProjectError(
        error instanceof Error
          ? error.message
          : 'No se pudieron cargar los proyectos.',
      );
      setProjectBusy(false);
    }
  }
  async function createProject(event: { preventDefault(): void }) {
    event.preventDefault();
    setProjectBusy(true);
    setProjectError('');
    try {
      let baseDirectory: string | undefined;
      if (!editingProject) {
        const directoryResponse = await fetch(
          `${API_BASE}/choose-project-directory`,
          { method: 'POST' },
        );
        const directoryBody = (await directoryResponse.json()) as {
          directory?: string | null;
          cancelled?: boolean;
          error?: string;
        };
        if (!directoryResponse.ok)
          throw new Error(
            directoryBody.error || 'No se pudo seleccionar la carpeta.',
          );
        if (directoryBody.cancelled || !directoryBody.directory) {
          setProjectBusy(false);
          return;
        }
        baseDirectory = directoryBody.directory;
      }
      const response = await fetch(
        `${API_BASE}/${editingProject ? 'update-project' : 'projects'}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            projectId: editingProject,
            name: newProjectName,
            description: projectDescription,
            baseDirectory,
          }),
        },
      );
      const body = (await response.json()) as {
        project: SimulationProject;
        error?: string;
      };
      if (!response.ok)
        throw new Error(body.error || 'No se pudo crear el proyecto.');
      if (editingProject) {
        setProjects((items) =>
          items.map((project) =>
            project.id === editingProject ? body.project : project,
          ),
        );
        setEditingProject(null);
        setNewProjectName('');
        setProjectDescription('');
        setProjectBusy(false);
      } else {
        setProjects((items) => [...items, body.project]);
        await chooseSimulation(body.project.id, '');
      }
    } catch (error) {
      setProjectError(
        error instanceof Error
          ? error.message
          : 'No se pudo crear el proyecto.',
      );
      setProjectBusy(false);
    }
  }
  useEffect(() => {
    void loadProjects();
  }, []);
  async function uploadCharmmGui(file?: File) {
    if (!file) return;
    if (!projectId) {
      setProjectError(
        'Crea o selecciona un proyecto antes de cargar el archivo.',
      );
      return;
    }
    setUploadState('uploading');
    setUploadMessage(`Preparando ${file.name}…`);
    setUploadLogs([
      `[${logClock()}] Archivo seleccionado: ${file.name}`,
      `[${logClock()}] Tamaño: ${(file.size / 1024 / 1024).toFixed(1)} MB`,
      `[${logClock()}] Iniciando transferencia al pipeline local…`,
    ]);
    try {
      setUploadLogs((lines) => [
        ...lines,
        `[${logClock()}] Leyendo el archivo seleccionado…`,
      ]);
      setUploadLogs((lines) => [
        ...lines,
        `[${logClock()}] Enviando archivo sin cargar una copia completa en memoria…`,
      ]);
      const result = await new Promise<{
        error?: string;
        namdDir?: string;
        simulation?: Simulation;
        report?: {
          dependency_file_count?: number;
          readmes_created?: string[];
          stages_created_or_updated?: string[];
        };
      }>((resolve, reject) => {
        const request = new XMLHttpRequest();
        let reported = -1;
        request.open('POST', `${API_BASE}/upload`);
        request.setRequestHeader('Content-Type', 'application/gzip');
        request.setRequestHeader('X-Filename', encodeURIComponent(file.name));
        request.setRequestHeader('X-Project-Id', projectId);
        request.timeout = 360000;
        let stalled = window.setTimeout(() => request.abort(), 30000);
        request.upload.addEventListener('progress', () => {
          clearTimeout(stalled);
          stalled = window.setTimeout(() => request.abort(), 30000);
        });
        request.upload.addEventListener('load', () => clearTimeout(stalled));
        request.addEventListener('loadend', () => clearTimeout(stalled));
        request.onabort = () =>
          reject(
            new Error(
              'La transferencia no avanzó durante 30 segundos. El archivo no se terminó de cargar.',
            ),
          );
        request.ontimeout = () =>
          reject(
            new Error(
              'La preparación superó el límite de 6 minutos. Revisa la consola del servicio local.',
            ),
          );
        request.upload.onprogress = (event) => {
          if (!event.lengthComputable) return;
          const percent = Math.floor((event.loaded / event.total) * 100);
          const bucket = Math.floor(percent / 10) * 10;
          if (bucket !== reported) {
            reported = bucket;
            setUploadLogs((lines) => [
              ...lines,
              `[${logClock()}] Carga: ${Math.min(bucket, 100)}%`,
            ]);
          }
        };
        request.upload.onload = () =>
          setUploadLogs((lines) => [
            ...lines,
            `[${logClock()}] Transferencia completa. Validando y extrayendo CHARMM-GUI…`,
          ]);
        request.onerror = () =>
          reject(new Error('No se pudo conectar con la API local de carga.'));
        request.onload = () => {
          let body: any = {};
          try {
            body = JSON.parse(request.responseText);
          } catch {}
          if (request.status >= 200 && request.status < 300) resolve(body);
          else reject(new Error(body.error || `Error HTTP ${request.status}`));
        };
        request.send(file);
      });
      if (result.simulation) {
        const simulation = result.simulation;
        setProjects((items) =>
          items.map((project) =>
            project.id === projectId
              ? {
                  ...project,
                  simulations: [...project.simulations, simulation],
                }
              : project,
          ),
        );
        setSimulationId(simulation.id);
        setData(null);
        setSelectedStage('auto');
      }
      setUploadLogs((lines) => [
        ...lines,
        `[${logClock()}] Carpeta NAMD localizada y dependencias analizadas.`,
        `[${logClock()}] Dependencias externas copiadas: ${result.report?.dependency_file_count ?? 0}.`,
        `[${logClock()}] Etapas generadas: ${result.report?.stages_created_or_updated?.join(', ') ?? 'completadas'}.`,
        `[${logClock()}] README creados: ${result.report?.readmes_created?.join(', ') ?? 'completados'}.`,
        `[${logClock()}] Preparación terminada correctamente.`,
        `[${logClock()}] Salida: ${result.namdDir}`,
      ]);
      setUploadState('success');
      setUploadMessage(`Preparado en ${result.namdDir}`);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Error de carga';
      setUploadLogs((lines) => [...lines, `[${logClock()}] ERROR: ${message}`]);
      setUploadState('error');
      setUploadMessage(message);
    } finally {
      if (fileInput.current) fileInput.current.value = '';
    }
  }
  async function importProject() {
    setProjectBusy(true);
    setProjectError('');
    try {
      const directoryResponse = await fetch(
        `${API_BASE}/choose-project-directory`,
        { method: 'POST' },
      );
      const directoryBody = (await directoryResponse.json()) as {
        directory?: string | null;
        cancelled?: boolean;
        error?: string;
      };
      if (!directoryResponse.ok)
        throw new Error(directoryBody.error || 'No se pudo seleccionar la carpeta.');
      if (directoryBody.cancelled || !directoryBody.directory) return;
      const response = await fetch(`${API_BASE}/import-project`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ directory: directoryBody.directory }),
      });
      const body = (await response.json()) as {
        project?: SimulationProject;
        error?: string;
      };
      if (!response.ok || !body.project)
        throw new Error(body.error || 'No se pudo cargar el proyecto.');
      await loadProjects(body.project.id, body.project.simulations[0]?.id);
    } catch (error) {
      setProjectError(
        error instanceof Error ? error.message : 'No se pudo cargar el proyecto.',
      );
    } finally {
      setProjectBusy(false);
    }
  }
  async function deleteSimulation() {
    if (!selectedSimulation || !selectedProject) return;
    if (!window.confirm(`¿Eliminar “${selectedSimulation.name}”? Se borrarán el paquete CHARMM-GUI, los archivos preparados y los resultados locales.`)) return;
    setProjectBusy(true);
    setProjectError('');
    try {
      const response = await fetch(
        `${API_BASE}/projects/${projectId}/simulations/${selectedSimulation.id}`,
        { method: 'DELETE' },
      );
      const body = (await response.json()) as { error?: string };
      if (!response.ok) throw new Error(body.error || 'No se pudo eliminar la simulación.');
      const remaining = selectedProject.simulations.filter(
        (item) => item.id !== selectedSimulation.id,
      );
      setProjects((items) =>
        items.map((project) =>
          project.id === projectId ? { ...project, simulations: remaining } : project,
        ),
      );
      setData(null);
      setSimulationId(remaining[0]?.id ?? '');
      if (remaining[0]) await chooseSimulation(projectId, remaining[0].id);
      setUploadMessage('Simulación eliminada.');
      setUploadState('success');
    } catch (error) {
      setProjectError(error instanceof Error ? error.message : 'No se pudo eliminar la simulación.');
    } finally {
      setProjectBusy(false);
    }
  }
  async function runPreparation() {
    if (!selectedSimulation) {
      setUploadState('error');
      setUploadMessage('Primero carga una simulación CHARMM-GUI.');
      return;
    }
    setRunState('running');
    setUploadState('uploading');
    setUploadMessage('Ejecutando README_preparacion…');
    setUploadLogs([
      `[${logClock()}] Solicitando csh README_preparacion`,
      `[${logClock()}] Carpeta: ${selectedSimulation.namdDir}`,
    ]);
    try {
      const response = await fetch(`${API_BASE}/run-preparation`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectId, simulationId, targetId }),
      });
      const result = (await response.json()) as {
        jobId?: string;
        pid?: number;
        stepCountsAligned?: string[];
        status?: string;
        skippedStages?: string[];
        pendingStages?: string[];
        error?: string;
        targetName?: string;
        remoteDir?: string;
      };
      if (!response.ok)
        throw new Error(result.error || 'No se pudo iniciar la preparación.');
      if (result.status === 'already-completed') {
        await loadProjects(projectId, simulationId);
        setRunState('idle');
        setUploadState('success');
        setUploadMessage('Todas las etapas ya terminaron correctamente; no se repitió ninguna.');
        setUploadLogs([
          `[${logClock()}] Simulación ya completada.`,
          ...(result.skippedStages ?? []).map((stage) => `[SKIP] ${stage} · terminada correctamente`),
        ]);
        return;
      }
      if (!result.jobId)
        throw new Error(result.error || 'No se pudo iniciar la preparación.');
      setCurrentJobId(result.jobId);
      setUploadLogs((lines) => [
        ...lines,
        `[${logClock()}] Destino: ${result.targetName}`,
        ...(result.pid ? [`[PID] Proceso lanzador: ${result.pid}`] : []),
        ...(result.stepCountsAligned?.length
          ? [`[NAMD] Pasos recalibrados: ${result.stepCountsAligned.join(', ')}`]
          : []),
        ...(result.skippedStages ?? []).map(
          (stage) => `[SKIP] ${stage} · terminada correctamente`,
        ),
        ...(result.remoteDir
          ? [`[${logClock()}] Resultados remotos: ${result.remoteDir}`]
          : []),
      ]);
      setUploadLogs((lines) => [
        ...lines,
        `[${logClock()}] Proceso iniciado. Leyendo salida en tiempo real…`,
      ]);
      if (runPollTimer.current) clearInterval(runPollTimer.current);
      runPollTimer.current = setInterval(async () => {
        try {
          const status = (await fetch(
            `${API_BASE}/run-status/${result.jobId}?t=${Date.now()}`,
            { cache: 'no-store' },
          ).then((r) => r.json())) as {
            status: string;
            pid?: number;
            slurmJobId?: string | null;
            syncError?: string;
            returncode: number | null;
            lines: string[];
            tail: string;
            activeOutput: string | null;
            stepsVisible: boolean;
            completedTails: { file: string; tail: string }[];
          };
          const completedLines = (status.completedTails ?? []).flatMap(
            (item) => [
              `[FIN] ${item.file} · últimos 400 caracteres`,
              ...item.tail
                .split(/\r?\n/)
                .filter(Boolean)
                .map((line) => `[TAIL] ${line}`),
            ],
          );
          const tailLines = status.tail
            .split(/\r?\n/)
            .filter(Boolean)
            .map((line) => `[TAIL ACTIVO] ${line}`);
          const progress = tailLines.length
            ? tailLines
            : status.stepsVisible && status.activeOutput
              ? [
                  `[NAMD] ${status.activeOutput} · graficando ENERGY en tiempo real…`,
                ]
              : [];
          setUploadLogs((current) =>
            [
              ...current
                .filter(
                  (line) =>
                    !line.startsWith('[NAMD]') &&
                    !line.startsWith('[TAIL') &&
                    !line.startsWith('[FIN]') &&
                    !line.startsWith('[SYNC]') &&
                    !line.startsWith('[SLURM]'),
                )
                .slice(0, 6),
              ...(status.slurmJobId
                ? [`[SLURM] Job ID: ${status.slurmJobId}`]
                : []),
              ...status.lines.map((line) => `[NAMD] ${line}`),
              ...completedLines,
              ...progress,
              ...(status.syncError ? [`[SYNC] ${status.syncError}`] : []),
            ].slice(-160),
          );
          if (status.status === 'stopping') setRunState('stopping');
          if (!['running', 'stopping'].includes(status.status)) {
            if (runPollTimer.current) clearInterval(runPollTimer.current);
            runPollTimer.current = null;
            setCurrentJobId(null);
            setRunState('idle');
            setUploadState(
              status.status === 'finished' || status.status === 'stopped'
                ? 'success'
                : 'error',
            );
            setUploadMessage(
              status.status === 'finished'
                ? 'Preparación completada.'
                : status.status === 'stopped'
                  ? 'Simulación detenida por el usuario.'
                  : `La preparación terminó con código ${status.returncode}.`,
            );
          }
        } catch {}
      }, 2000);
    } catch (error) {
      const message =
        error instanceof Error ? error.message : 'Error al ejecutar';
      setRunState('idle');
      setUploadState('error');
      setUploadMessage(message);
      setUploadLogs((lines) => [...lines, `[${logClock()}] ERROR: ${message}`]);
    }
  }
  async function stopSimulation() {
    if (!currentJobId || runState === 'stopping') return;
    setRunState('stopping');
    setUploadMessage('Deteniendo la simulación…');
    setUploadLogs((lines) => [
      ...lines,
      `[${logClock()}] STOP solicitado. Terminando NAMD de esta simulación…`,
    ]);
    try {
      const response = await fetch(`${API_BASE}/stop-preparation`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ jobId: currentJobId }),
      });
      const result = (await response.json()) as { error?: string };
      if (!response.ok)
        throw new Error(result.error || 'No se pudo detener la simulación.');
    } catch (error) {
      const message =
        error instanceof Error ? error.message : 'Error al detener';
      setRunState('running');
      setUploadState('error');
      setUploadMessage(message);
      setUploadLogs((lines) => [...lines, `[${logClock()}] ERROR: ${message}`]);
    }
  }
  useEffect(() => {
    if (!selectedSource || (uploadState === 'uploading' && runState === 'idle'))
      return;
    let cancelled = false;
    const controller = new AbortController();
    const applyData = (value: DashboardData) => {
      if (
        cancelled ||
        value.source.toLowerCase().replaceAll('\\', '/') !==
          selectedSource!.toLowerCase().replaceAll('\\', '/')
      )
        return;
      setData((previous) =>
        previous && dataRevision(previous) === dataRevision(value)
          ? previous
          : value,
      );
      setLoadError('');
    };
    async function loadSnapshot() {
      try {
        const response = await fetch(
          `${API_BASE}/dashboard-data?t=${Date.now()}`,
          { cache: 'no-store', signal: controller.signal },
        );
        if (!response.ok) throw new Error(String(response.status));
        applyData((await response.json()) as DashboardData);
      } catch {
        if (!cancelled) setLoadError('No se pudo leer dashboard.json');
      }
    }
    void loadSnapshot();
    const events = new EventSource(`${API_BASE}/events`);
    events.addEventListener('dashboard', (event) => {
      try {
        applyData(JSON.parse((event as MessageEvent).data) as DashboardData);
      } catch {
        setLoadError('Se recibió un evento NAMD incompleto');
      }
    });
    events.onopen = () => setLoadError('');
    events.onerror = () => {
      if (!cancelled) setLoadError('Reconectando datos en tiempo real…');
    };
    // Recuperación eventual si el navegador o un proxy interrumpe SSE.
    const fallback = window.setInterval(() => {
      if (!document.hidden) void loadSnapshot();
    }, 10000);
    return () => {
      cancelled = true;
      controller.abort();
      events.close();
      window.clearInterval(fallback);
    };
  }, [uploadState, runState, selectedSource]);
  const meta = metricMeta[metric];
  const activeStage = useMemo(
    () =>
      data?.stages.findLast(
        (s) => s.status === 'running' || s.status === 'error',
      ) ?? data?.stages.at(-1),
    [data],
  );
  const selectedStageData = useMemo(
    () =>
      selectedStage === 'auto'
        ? undefined
        : data?.stages.find((stage) => stage.stage === selectedStage),
    [data, selectedStage],
  );
  const chartStage = useMemo(
    () =>
      selectedStageData ??
      (selectedStage === 'auto'
        ? (data?.stages.filter((s) => s.series.length).at(-1) ??
          data?.previousRun?.stages.filter((s) => s.series.length).at(-1))
        : undefined),
    [data, selectedStage, selectedStageData],
  );
  const series = chartStage?.series ?? [];
  const rmsdSeries = useMemo(
    () =>
      selectedStage === 'auto'
        ? (data?.rmsd.series ?? [])
        : (data?.rmsd.series ?? []).filter(
            (point) => point.stage === selectedStage,
          ),
    [data, selectedStage],
  );
  const chartSeries = useMemo(() => {
    const points =
      metric === 'rmsd'
        ? rmsdSeries.map((point, index) => ({
            step: point.step ?? index,
            rmsd: point.value,
          }))
        : series;
    return chartWindow(
      applyStride<Point | { step: number; rmsd: number }>(points, stride),
    );
  }, [metric, rmsdSeries, series, stride]);
  const last = data?.summary.last ?? chartStage?.series.at(-1) ?? null;
  const selectedLast =
    chartStage?.series.at(-1) ?? (selectedStage === 'auto' ? last : null);
  const lastRmsdPoint = rmsdSeries.at(-1);
  const lastRmsd = lastRmsdPoint?.value;
  const currentPoint = chartStage?.series.at(-1) ?? last;
  const visibleStage =
    metric === 'rmsd' ? lastRmsdPoint?.stage : chartStage?.stage;
  const visibleSource =
    metric === 'rmsd' ? lastRmsdPoint?.source : chartStage?.file;
  const waitingForActiveData =
    selectedStage === 'auto' &&
    Boolean(
      activeStage?.stage && visibleStage && activeStage.stage !== visibleStage,
    );
  const displayedStep =
    metric === 'rmsd' ? lastRmsdPoint?.step : currentPoint?.step;
  const displayStage = selectedStageData ?? activeStage;
  const displayedPointCount =
    selectedStage === 'auto'
      ? data?.summary.points
      : selectedStageData?.series.length;
  const agent = data?.agent;
  const baseStageSpec = [
    ['6.0', 'Minimización', 'step6.0_minimization'],
    ['6.1', 'Termalización', 'step6.1_thermalization'],
    ['6.2', 'Restricción 10.0', 'step6.2_equilibration'],
    ['6.3', 'Restricción 5.0', 'step6.3_equilibration'],
    ['6.4', 'NPT · 2.5', 'step6.4_equilibration'],
    ['6.5', 'NPT · 1.0', 'step6.5_equilibration'],
    ['6.6', 'NPT · 0.5', 'step6.6_equilibration'],
    ['6.7', 'NPT · 0.1', 'step6.7_equilibration'],
  ];
  const continuationSpec = agent?.continuation
    ? [
        [
          agent.continuation.stage.match(/^step(6\.\d+)/)?.[1] ?? '6.8',
          'Agente · NPT libre',
          agent.continuation.stage,
        ],
      ]
    : [];
  const stageSpec = [...baseStageSpec, ...continuationSpec];
  const stages = stageSpec.map(([id, name, key]) => {
    const found = data?.stages.find((s) => s.stage === key);
    return [id, name, found?.status ?? 'waiting', found?.seed] as const;
  });
  const unavailable = !chartSeries.some(
    (point) => point[metric as keyof typeof point] != null,
  );
  const agentIsPositive = agent?.status === 'ready';
  useEffect(() => {
    const context = document.modelContext;
    if (!context?.registerTool) return;
    const lifecycle = new AbortController();
    void Promise.resolve(
      context.registerTool(
        {
          name: 'set_dashboard_view',
          title: 'Cambiar vista del dashboard',
          description:
            'Selecciona la réplica y la métrica visibles en el dashboard de dinámica molecular.',
          inputSchema: {
            type: 'object',
            properties: {
              replica: {
                type: 'string',
                enum: ['Réplica 01', 'Réplica 02', 'Réplica 03'],
              },
              metric: {
                type: 'string',
                enum: [
                  'energy',
                  'electrostatic',
                  'temperature',
                  'pressure',
                  'rmsd',
                ],
              },
            },
            additionalProperties: false,
          },
          annotations: { readOnlyHint: false, untrustedContentHint: false },
          execute(input: unknown) {
            const value = input as { replica?: string; metric?: Metric };
            if (value.replica) setReplica(value.replica);
            if (value.metric) setMetric(value.metric);
            return {
              replica: value.replica ?? replica,
              metric: value.metric ?? metric,
            };
          },
        },
        { signal: lifecycle.signal },
      ),
    ).catch(() => undefined);
    return () => lifecycle.abort();
  }, [metric, replica]);
  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">
            <Atom />
          </span>
          <div>
            <strong>MD Control</strong>
            <small>NAMD pipeline</small>
          </div>
        </div>
        <nav aria-label="Navegación principal">
          <button
            className={`nav-item ${view === 'dashboard' ? 'active' : ''}`}
            onClick={() => setView('dashboard')}
          >
            <CircleGauge />
            Resumen
          </button>
          <button className="nav-item" onClick={() => setView('dashboard')}>
            <Activity />
            Métricas
          </button>
          <button className="nav-item" onClick={() => setView('dashboard')}>
            <FlaskConical />
            Réplicas
          </button>
          <button
            className={`nav-item ${view === 'ssh' ? 'active' : ''}`}
            onClick={() => setView('ssh')}
          >
            <Server />
            SSH
          </button>
        </nav>
        <div className="sidebar-foot">
          <span className="pulse" /> Análisis activo
        </div>
      </aside>
      {view === 'ssh' ? (
        <SshModule />
      ) : (
        <section className="workspace">
          <header className="topbar">
            <div>
              <p className="eyebrow">DATOS NAMD</p>
              <Select
                value={selectedStage}
                onValueChange={(value) => setSelectedStage(value ?? 'auto')}
              >
                <SelectTrigger
                  className="stage-select"
                  aria-label="Archivo NAMD visible"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="auto">
                    Automático · {activeStage?.stage ?? 'esperando'}
                  </SelectItem>
                  {data?.stages.map((stage) => (
                    <SelectItem key={stage.stage} value={stage.stage}>
                      {stage.stage}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="subtle" title={data?.source}>
                {data
                  ? `${(displayedPointCount ?? 0).toLocaleString('es-CL')} puntos · ${selectedStage === 'auto' ? `${data.summary.outputs} archivos .out` : (selectedStageData?.file ?? 'archivo no disponible')}`
                  : loadError || 'Cargando archivos…'}
              </p>
            </div>
            <div className="top-actions">
              <input
                ref={fileInput}
                className="file-input"
                type="file"
                accept=".tgz,.tar.gz,application/gzip"
                onChange={(event) =>
                  void uploadCharmmGui(event.target.files?.[0])
                }
              />
              <button
                className="upload-button"
                onClick={() => fileInput.current?.click()}
                disabled={
                  uploadState === 'uploading' ||
                  projectBusy ||
                  !projectId ||
                  runState !== 'idle'
                }
              >
                {uploadState === 'uploading' ? (
                  <LoaderCircle className="spin" />
                ) : (
                  <Upload />
                )}
                {uploadState === 'uploading'
                  ? 'Procesando…'
                  : 'Cargar CHARMM-GUI'}
              </button>
              <button
                className="text-button"
                onClick={() => void deleteSimulation()}
                disabled={
                  !selectedSimulation ||
                  uploadState === 'uploading' ||
                  projectBusy ||
                  runState !== 'idle'
                }
                title="Eliminar la simulación seleccionada y sus archivos locales"
              >
                <Trash2 />
                Eliminar simulación
              </button>
              <button
                className={`run-button ${runState !== 'idle' ? 'stop' : ''}`}
                onClick={() =>
                  void (runState === 'idle'
                    ? runPreparation()
                    : stopSimulation())
                }
                disabled={
                  runState === 'stopping' ||
                  (runState === 'idle' &&
                    (uploadState === 'uploading' ||
                      projectBusy ||
                      !selectedSimulation))
                }
              >
                {runState === 'stopping' ? (
                  <LoaderCircle className="spin" />
                ) : runState === 'running' ? (
                  <Square />
                ) : (
                  <Play />
                )}
                {runState === 'stopping'
                  ? 'DETENIENDO'
                  : runState === 'running'
                    ? 'STOP simulación'
                    : 'RUN preparación'}
              </button>
              <Select
                value={replica}
                onValueChange={(value) => setReplica(value ?? 'Réplica 01')}
              >
                <SelectTrigger className="replica-select">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="Réplica 01">Réplica 01</SelectItem>
                  <SelectItem value="Réplica 02">Réplica 02</SelectItem>
                  <SelectItem value="Réplica 03">Réplica 03</SelectItem>
                </SelectContent>
              </Select>
              <span
                className="timestep-pill"
                title="Tamaño del paso de integración leído desde la salida NAMD"
              >
                <Clock3 />
                <span>timestep</span>
                <strong>
                  {displayStage
                    ? `${displayStage.timestepFs.toLocaleString('es-CL')} fs`
                    : '—'}
                </strong>
              </span>
              <span className="status-pill">
                <span />
                {displayStage?.errors.length
                  ? `${displayStage.errors.length} error detectado`
                  : displayStage?.status === 'done'
                    ? 'etapa completada'
                    : displayStage?.status === 'running'
                      ? 'leyendo datos'
                      : 'etapa pendiente'}
              </span>
            </div>
          </header>
          <div className="upload-module">
            {uploadState !== 'idle' && (
              <div className={`upload-feedback ${uploadState}`}>
                {uploadState === 'uploading' ? (
                  <LoaderCircle className="spin" />
                ) : uploadState === 'success' ? (
                  <CheckCircle2 />
                ) : (
                  <AlertTriangle />
                )}
                <span>{uploadMessage}</span>
                {uploadState !== 'uploading' && (
                  <button onClick={() => setUploadState('idle')}>×</button>
                )}
              </div>
            )}
              <div className={`upload-console ${uploadState === 'idle' ? 'standalone' : ''}`}>
                <div className="console-head">
                  <span>
                    <i /> PIPELINE LOG
                  </span>
                  <button onClick={() => setUploadLogs([])}>Limpiar</button>
                </div>
                <div className="console-body">
                  {uploadLogs.length ? (
                    uploadLogs.map((line, index) => (
                      <div
                        key={`${index}-${line}`}
                        className={
                          line.includes('ERROR:')
                            ? 'log-error'
                            : line.includes('terminada correctamente')
                              ? 'log-success'
                              : ''
                        }
                      >
                        {line}
                      </div>
                    ))
                  ) : (
                    <div className="log-muted">Esperando eventos…</div>
                  )}
                </div>
              </div>
            </div>
          <article className="panel project-module">
            <div>
              <p className="eyebrow">PROYECTO → SIMULACIÓN</p>
              <h2>Proyectos y simulaciones</h2>
              <p>
                Cada archivo CHARMM-GUI .tgz crea una simulación en el proyecto
                seleccionado.
              </p>
            </div>
            <div className="project-fields">
              <label>
                Proyecto
                <select
                  aria-label="Proyecto"
                  value={projectId}
                  disabled={
                    projectBusy ||
                    uploadState === 'uploading' ||
                    runState !== 'idle'
                  }
                  onChange={(e) => {
                    const project = projects.find(
                      (p) => p.id === e.target.value,
                    );
                    void chooseSimulation(
                      e.target.value,
                      project?.simulations[0]?.id ?? '',
                    );
                  }}
                >
                  <option value="" disabled>
                    {projects.length
                      ? 'Selecciona un proyecto'
                      : 'Crea tu primer proyecto'}
                  </option>
                  {projects.map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.name} ({project.simulations.length})
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Simulación
                <select
                  aria-label="Simulación"
                  value={simulationId}
                  disabled={
                    projectBusy ||
                    uploadState === 'uploading' ||
                    runState !== 'idle' ||
                    !selectedProject?.simulations.length
                  }
                  onChange={(e) =>
                    void chooseSimulation(projectId, e.target.value)
                  }
                >
                  <option value="" disabled>
                    Selecciona una simulación
                  </option>
                  {selectedProject?.simulations.map((simulation) => (
                    <option key={simulation.id} value={simulation.id}>
                      {simulation.name} · {simulation.state?.progressPercent ?? 0}% ·{' '}
                      {simulation.id.slice(0, 6)}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            {selectedProject && (
              <div className="project-description">
                <h3>{selectedProject.name}</h3>
                <p style={{ whiteSpace: 'pre-wrap' }}>
                  {selectedProject.description || 'Sin descripción'}
                </p>
                {selectedProject.directory && (
                  <small title={selectedProject.directory}>
                    Ubicación: {selectedProject.directory}
                  </small>
                )}
                {selectedSimulation?.state && (
                  <div className="simulation-state" aria-live="polite">
                    <strong>
                      Paso actual:{' '}
                      {selectedSimulation.state.currentStep ?? 'sin iniciar'}
                    </strong>
                    <span>
                      {selectedSimulation.state.progressPercent}% ·{' '}
                      {selectedSimulation.state.completedSteps.length} terminadas ·{' '}
                      {selectedSimulation.state.pendingSteps.length} pendientes
                    </span>
                    <progress
                      max={100}
                      value={selectedSimulation.state.progressPercent}
                    />
                  </div>
                )}
                <button
                  className="text-button"
                  disabled={
                    projectBusy ||
                    uploadState === 'uploading' ||
                    runState !== 'idle'
                  }
                  onClick={() => {
                    setEditingProject(selectedProject.id);
                    setNewProjectName(selectedProject.name);
                    setProjectDescription(selectedProject.description);
                  }}
                >
                  Editar proyecto
                </button>
              </div>
            )}
            <form
              className="project-create"
              onSubmit={(event) => void createProject(event)}
            >
              <input
                aria-label="Nombre del proyecto"
                placeholder="Nombre del nuevo proyecto"
                required
                maxLength={100}
                value={newProjectName}
                disabled={
                  projectBusy ||
                  uploadState === 'uploading' ||
                  runState !== 'idle'
                }
                onChange={(e) => setNewProjectName(e.target.value)}
              />
              <textarea
                aria-label="Descripción del proyecto"
                placeholder="Descripción del proyecto"
                rows={3}
                maxLength={4000}
                value={projectDescription}
                disabled={
                  projectBusy ||
                  uploadState === 'uploading' ||
                  runState !== 'idle'
                }
                onChange={(e) => setProjectDescription(e.target.value)}
              />
              <button
                className="upload-button"
                disabled={
                  projectBusy ||
                  uploadState === 'uploading' ||
                  runState !== 'idle' ||
                  !newProjectName.trim()
                }
              >
                {editingProject ? <Save /> : <Plus />}
                {editingProject ? 'Guardar proyecto' : 'Crear proyecto'}
              </button>
              {editingProject && (
                <button
                  type="button"
                  className="text-button"
                  disabled={projectBusy}
                  onClick={() => {
                    setEditingProject(null);
                    setNewProjectName('');
                    setProjectDescription('');
                  }}
                >
                  Cancelar
                </button>
              )}
            </form>
            <button
              type="button"
              className="text-button"
              disabled={projectBusy || uploadState === 'uploading' || runState !== 'idle'}
              onClick={() => void importProject()}
            >
              <Upload />
              Cargar proyecto desde config.ini
            </button>
            {projectError && (
              <p role="alert">
                {projectError}{' '}
                <button
                  className="text-button"
                  disabled={
                    projectBusy ||
                    uploadState === 'uploading' ||
                    runState !== 'idle'
                  }
                  onClick={() => void loadProjects(projectId, simulationId)}
                >
                  Reintentar
                </button>
              </p>
            )}
            {selectedProject && !selectedProject.simulations.length && (
              <p>
                Este proyecto todavía no tiene simulaciones. Usa «Cargar
                CHARMM-GUI» para agregar la primera.
              </p>
            )}
            {selectedSimulation && (
              <p title={selectedSimulation.namdDir}>
                Simulación: <strong>{selectedSimulation.name}</strong> ·
                Archivo: {selectedSimulation.filename ?? 'Carga anterior'}
              </p>
            )}
          </article>
          <ExecutionModule
            value={targetId}
            onChange={(id) => {
              setTargetId(id);
              window.localStorage.setItem(EXECUTION_TARGET_STORAGE_KEY, id);
            }}
            disabled={runState !== 'idle' || uploadState === 'uploading'}
            onManage={() => setView('ssh')}
          />
          <div className="content-grid">
            <section className="main-column">
              <div className="kpi-row">
                <article className="kpi">
                  <div className="kpi-icon green">
                    <Waves />
                  </div>
                  <div>
                    <span>Energía potencial</span>
                    <strong>
                      {selectedLast?.energy?.toLocaleString('es-CL', {
                        maximumFractionDigits: 1,
                      }) ?? '—'}
                    </strong>
                    <small>kcal/mol · desde ENERGY</small>
                  </div>
                </article>
                <article className="kpi">
                  <div className="kpi-icon amber">
                    <Thermometer />
                  </div>
                  <div>
                    <span>Temperatura</span>
                    <strong>
                      {selectedLast?.temperature?.toFixed(2) ?? '—'}
                    </strong>
                    <small>K · dato real de NAMD</small>
                  </div>
                </article>
                <article className="kpi">
                  <div className="kpi-icon violet">
                    <Activity />
                  </div>
                  <div>
                    <span>RMSD proteína</span>
                    <strong>
                      {lastRmsd != null ? lastRmsd.toFixed(3) : 'pendiente'}
                    </strong>
                    <small>Å · desde snapshots .coor.old alineados</small>
                  </div>
                </article>
                <article className="kpi">
                  <div className="kpi-icon blue">
                    <CircleGauge />
                  </div>
                  <div>
                    <span>Presión instantánea</span>
                    <strong>{selectedLast?.pressure?.toFixed(2) ?? '—'}</strong>
                    <small>bar · desde ENERGY</small>
                  </div>
                </article>
              </div>
              <article className="panel chart-panel">
                <div className="panel-head">
                  <div>
                    <p className="eyebrow">SERIE TEMPORAL</p>
                    <h2>{meta.label}</h2>
                  </div>
                  <div className="chart-controls">
                    <div
                      className="metric-tabs"
                      role="group"
                      aria-label="Métrica del gráfico"
                    >
                      {(Object.keys(metricMeta) as Metric[]).map((key) => (
                        <button
                          key={key}
                          onClick={() => setMetric(key)}
                          className={metric === key ? 'selected' : ''}
                        >
                          {metricMeta[key].label.split(' ')[0]}
                        </button>
                      ))}
                    </div>
                    <Select
                      value={String(stride)}
                      onValueChange={(value) => setStride(Number(value ?? 1))}
                    >
                      <SelectTrigger
                        className="stride-select"
                        aria-label="Stride del gráfico"
                      >
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {[1, 2, 5, 10, 20, 50].map((value) => (
                          <SelectItem key={value} value={String(value)}>
                            Stride {value}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                </div>
                <div className="chart-wrap">
                  {unavailable ? (
                    <div className="empty-state">
                      <Activity />
                      <strong>Métrica aún no calculada</strong>
                      <span>
                        {metric === 'rmsd'
                          ? data?.rmsd.reason
                          : 'El archivo actual no contiene esta columna.'}
                      </span>
                    </div>
                  ) : (
                    <ResponsiveContainer width="100%" height="100%">
                      <LineChart
                        data={chartSeries}
                        margin={{ top: 12, right: 12, left: 2, bottom: 0 }}
                      >
                        <CartesianGrid
                          stroke="#22334b"
                          strokeDasharray="2 5"
                          vertical={false}
                        />
                        <XAxis
                          dataKey="step"
                          stroke="#71829a"
                          tickLine={false}
                          axisLine={false}
                          tick={{ fontSize: 12 }}
                        />
                        <YAxis
                          domain={meta.domain ?? ['auto', 'auto']}
                          stroke="#71829a"
                          tickLine={false}
                          axisLine={false}
                          tick={{ fontSize: 12 }}
                          width={82}
                        />
                        <Tooltip content={<MetricTooltip metric={metric} />} />
                        {meta.target && (
                          <ReferenceLine
                            y={meta.target}
                            stroke="#ffbf69"
                            strokeDasharray="5 5"
                            opacity={0.55}
                          />
                        )}
                        <Line
                          type="monotone"
                          dataKey={metric}
                          stroke={meta.color}
                          strokeWidth={2.4}
                          dot={false}
                          isAnimationActive={false}
                          activeDot={{ r: 4, fill: meta.color }}
                        />
                      </LineChart>
                    </ResponsiveContainer>
                  )}
                </div>
                <div className="chart-note">
                  <span
                    className={data?.summary.errors ? 'error-dot' : 'good-dot'}
                  />{' '}
                  Etapa activa: {activeStage?.stage ?? 'esperando'} · Datos
                  visibles: {visibleSource ?? 'sin datos'}
                  {waitingForActiveData
                    ? ' · inicializando etapa, se conserva la serie anterior'
                    : ''}{' '}
                  <span>
                    Paso graficado:{' '}
                    {displayedStep?.toLocaleString('es-CL') ?? '—'} · stride{' '}
                    {stride}
                  </span>
                </div>
              </article>
              <article className="panel rmsf-panel">
                <div className="panel-head">
                  <div>
                    <p className="eyebrow">FLEXIBILIDAD POR RESIDUO</p>
                    <h2>RMSF</h2>
                  </div>
                  <span className="muted-chip">
                    {data?.rmsf.status === 'ready'
                      ? 'Cα · tiempo real'
                      : 'Pendiente'}
                  </span>
                </div>
                {data?.rmsf.series?.length ? (
                  <div className="mini-chart">
                    <ResponsiveContainer width="100%" height="100%">
                      <LineChart data={data.rmsf.series}>
                        <CartesianGrid
                          stroke="#22334b"
                          strokeDasharray="2 5"
                          vertical={false}
                        />
                        <XAxis
                          dataKey="residue"
                          stroke="#71829a"
                          tickLine={false}
                          axisLine={false}
                          tick={{ fontSize: 11 }}
                        />
                        <YAxis
                          stroke="#71829a"
                          tickLine={false}
                          axisLine={false}
                          tick={{ fontSize: 11 }}
                          width={35}
                        />
                        <Tooltip
                          contentStyle={{
                            background: '#111d2e',
                            border: '1px solid #2b405c',
                            borderRadius: 10,
                          }}
                        />
                        <Line
                          dataKey="value"
                          type="monotone"
                          stroke="#d0bfff"
                          strokeWidth={2}
                          dot={false}
                          isAnimationActive={false}
                        />
                      </LineChart>
                    </ResponsiveContainer>
                  </div>
                ) : (
                  <div className="mini-chart empty-state">
                    <Activity />
                    <strong>Esperando más snapshots</strong>
                    <span>{data?.rmsf.reason}</span>
                  </div>
                )}
              </article>
            </section>
            <aside className="right-column">
              <article
                className={`panel agent-card agent-${agent?.status ?? 'waiting'}`}
              >
                <div className="agent-title">
                  <span>{agentIsPositive ? <CheckCircle2 /> : <Atom />}</span>
                  <div>
                    <p className="eyebrow">AGENTE DE EQUILIBRACIÓN</p>
                    <h2>{agent?.decision ?? 'Esperando datos'}</h2>
                  </div>
                </div>
                {agent?.metrics.atomCount != null && (
                  <div className="agent-facts">
                    <span>
                      {agent.metrics.atomCount.toLocaleString('es-CL')} átomos
                    </span>
                    <span>
                      ventana mínima{' '}
                      {agent.metrics.minimumWindowPs?.toLocaleString('es-CL')}{' '}
                      ps
                    </span>
                    <span>
                      {agent.metrics.timestepFs?.toLocaleString('es-CL')} fs →
                      objetivo{' '}
                      {agent.metrics.targetTimestepFs?.toLocaleString('es-CL')}{' '}
                      fs
                    </span>
                  </div>
                )}
                <p>
                  {agent?.reasons[0] ??
                    'El análisis comienza automáticamente al terminar step6.7.'}
                </p>
                {agent?.reasons && agent.reasons.length > 1 && (
                  <ul className="agent-reasons">
                    {agent.reasons.slice(1, 4).map((reason) => (
                      <li key={reason}>{reason}</li>
                    ))}
                  </ul>
                )}
                <div className="confidence">
                  <div>
                    <span>Confianza de la decisión</span>
                    <strong>{agent?.confidence ?? 'sin evidencia'}</strong>
                  </div>
                  <div className="confidence-track">
                    <span
                      style={{
                        width:
                          agent?.confidence === 'alta'
                            ? '100%'
                            : agent?.confidence === 'baja'
                              ? '40%'
                              : '8%',
                      }}
                    />
                  </div>
                </div>
                <div className="agent-action">
                  {agent?.status === 'error' ? (
                    <AlertTriangle />
                  ) : agentIsPositive ? (
                    <CheckCircle2 />
                  ) : (
                    <Activity />
                  )}
                  <div>
                    <strong>Siguiente acción</strong>
                    <span>
                      {agent?.nextAction ??
                        'Completar la preparación hasta step6.7.'}
                    </span>
                  </div>
                  <ChevronRight />
                </div>
              </article>
              <article className="panel stages-card">
                <div className="panel-head compact">
                  <div>
                    <p className="eyebrow">PREPARACIÓN</p>
                    <h2>Etapas step6.x</h2>
                  </div>
                  <span className="progress-label">
                    {stages.filter((s) => s[2] === 'done').length} /{' '}
                    {stages.length}
                  </span>
                </div>
                <div className="stage-list">
                  {stages.map(([id, name, state, seed]) => (
                    <div className={`stage ${state}`} key={id}>
                      <span className="stage-node">
                        {state === 'done' ? '✓' : id}
                      </span>
                      <div>
                        <strong>step{id}</strong>
                        <small>
                          {name} · semilla:{' '}
                          {seed ??
                            (id === '6.0' ? 'no aplica' : 'no registrada')}
                        </small>
                      </div>
                      {state === 'running' && (
                        <span className="running-label">EN CURSO</span>
                      )}
                      {state === 'error' && (
                        <span className="running-label">ERROR</span>
                      )}
                    </div>
                  ))}
                </div>
              </article>
              <article className="panel alert-card">
                <AlertTriangle />
                <div>
                  <strong>
                    {agent?.reasons.length ?? data?.summary.errors ?? 0}{' '}
                    observaciones del agente
                  </strong>
                  <p>
                    {agent?.status === 'ready'
                      ? 'Los criterios configurados se cumplen en la ventana analizada.'
                      : (agent?.reasons[0] ??
                        data?.errors[0]?.message ??
                        'Esperando la evaluación final.')}
                  </p>
                </div>
              </article>
              <div className="run-meta">
                <div>
                  <Clock3 />
                  <span>
                    Datos generados
                    <strong>
                      {data
                        ? new Date(data.generatedAt).toLocaleString('es-CL')
                        : '—'}
                    </strong>
                  </span>
                </div>
                <div>
                  <FlaskConical />
                  <span>
                    Semilla NAMD
                    <strong>{displayStage?.seed ?? 'no registrada'}</strong>
                  </span>
                </div>
              </div>
            </aside>
          </div>
        </section>
      )}
    </main>
  );
}
