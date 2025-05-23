import os
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
from torch.nn.parallel import DataParallel
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.cuda.amp import GradScaler, autocast  # Per mixed-precision
import rasterio
from rasterio.errors import RasterioIOError
from skimage.metrics import structural_similarity as ssim
from sklearn.metrics import confusion_matrix
from pytorch_msssim import SSIM
from PIL import Image
import torchvision.transforms as transforms
import glob

# === Configurazione multi-GPU ===
def get_device():
    if torch.cuda.device_count() > 1:
        print(f"Using {torch.cuda.device_count()} GPUs!")
        return torch.device("cuda")
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")

# === Configurazione base ===
DEVICE = get_device()
NUM_WORKERS = 8
BATCH_SIZE = 4
LEARNING_RATE = 0.001
NUM_EPOCHS = 100
INPUT_LENGTH = 6
PRED_LENGTH = 6
LAMBDA_DECOUPLE = 0.001

# Normalizzazione delle immagini
def normalize_image(img):
    # Verifica che i valori siano validi
    img = np.nan_to_num(img, nan=0.0, posinf=0.0, neginf=0.0)
    img = np.clip(img, 0, None)  # Assicurati che non ci siano valori negativi
    
    # 1. Converti in dBZ (riflettività)
    img = 10 * np.log1p(img + 1e-8)  # Aggiungi epsilon per evitare log(0)
    
    # 2. Normalizza nel range fisso [0, 70] dBZ (valori tipici per radar meteorologici)
    min_dbz, max_dbz = 0, 70
    img = np.clip(img, min_dbz, max_dbz)  # Limita ai valori fisici del dataset
    img = (img - min_dbz) / (max_dbz - min_dbz)  # Scala tra 0 e 1
    
    # 3. Gestisci valori non fisici
    img = np.nan_to_num(img, nan=0.0, posinf=1.0, neginf=0.0)
    return img

# === Inizializzazione seed ===
def set_seed(seed=15):
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = False
    torch.backends.cudnn.benchmark = True
    os.environ['PYTORCH_CUDA_ALLOC_CONF'] = 'max_split_size_mb:128'

set_seed()

# === Dataset ===
class RadarDataset(Dataset):
    def __init__(self, data_path, input_length=6, pred_length=6, is_train=True):
        self.input_length = input_length
        self.pred_length = pred_length
        self.seq_length = input_length + pred_length
        self.files = sorted(glob.glob(os.path.join(data_path, '**/*.tiff'), recursive=True))
        self.is_train = is_train
        self.transform = transforms.Compose([
            # transforms.Resize((600, 700)),
            transforms.CenterCrop((350, 500)),
            transforms.ToTensor(),
        ])
        # Cache per tenere traccia della validità dei file
        self.file_validity = {}
        
        # Calcolo finestre valide
        self.valid_indices = []
        self.total_possible_windows = max(0, len(self.files) - self.seq_length + 1)
        
        for start_idx in range(self.total_possible_windows):
            window_valid = True
            for i in range(self.seq_length):
                file = self.files[start_idx + i]
                
                # Verifica validità del file solo se non già controllato
                if file not in self.file_validity:
                    try:
                        with rasterio.open(file) as src:
                            valid = src.count > 0
                    except RasterioIOError:
                        valid = False
                        print(f"File non valido: {file}")
                    self.file_validity[file] = valid
                
                if not self.file_validity[file]:
                    window_valid = False
                    break  # Interrompe il ciclo alla prima occorrenza di file non valido
                
            if window_valid:
                self.valid_indices.append(start_idx)
        
        # Statistiche finali
        self.total_files = len(self.files)
        self.invalid_files = sum(1 for valid in self.file_validity.values() if not valid)
        self.valid_windows = len(self.valid_indices)
        self.invalid_windows = self.total_possible_windows - self.valid_windows
        
        print(f"\nStatistiche Dataset:")
        print(f"1. File totali: {self.total_files}")
        print(f"2. File non validi: {self.invalid_files}")
        print(f"3. Finestre totali possibili: {self.total_possible_windows}")
        print(f"4. Finestre valide: {self.valid_windows}")
        print(f"5. Finestre non valide: {self.invalid_windows}")
        print(" ===================================================== \n")

    def __len__(self):
        return len(self.valid_indices)

    def __getitem__(self, idx):
        start = self.valid_indices[idx]
        images = []
        for i in range(self.seq_length):
            file = self.files[start + i]
            with rasterio.open(file) as src:
                img = src.read(1).astype(np.float32)
                img = normalize_image(img) #img = (img / 40.0).clip(0, 1)  # Normalizzazione
                img = Image.fromarray(img)
                img = self.transform(img)
                images.append(img)
        
        inputs = torch.stack(images[:self.input_length])
        targets = torch.stack(images[self.input_length:])
        return inputs, targets

# === Modello ===
class SpatiotemporalLSTMCell(nn.Module):
    def __init__(self, input_dim, hidden_dim, filter_size):
        super().__init__()
        self.hidden_dim = hidden_dim
        padding = filter_size // 2
        
        # Convoluzioni per input e hidden state
        self.conv_x = nn.Conv2d(input_dim, hidden_dim * 7, kernel_size=filter_size, padding=padding)
        self.conv_h = nn.Conv2d(hidden_dim, hidden_dim * 7, kernel_size=filter_size, padding=padding)
        
        # Convoluzioni per le due memorie
        self.conv_c = nn.Conv2d(hidden_dim, hidden_dim * 3, kernel_size=1)
        self.conv_m = nn.Conv2d(hidden_dim, hidden_dim * 3, kernel_size=1)

        # Fusione delle memorie C e M
        self.conv_fusion = nn.Conv2d(hidden_dim * 2, hidden_dim, kernel_size=1)
        
        # **Due convoluzioni separate per il decoupling**
        self.conv_decouple_c = nn.Conv2d(hidden_dim, hidden_dim, kernel_size=1)
        self.conv_decouple_m = nn.Conv2d(hidden_dim, hidden_dim, kernel_size=1)

    def forward(self, x, h_prev, c_prev, m_prev, m_upper=None):
        # Se x ha dimensioni diverse da h_prev, ridimensionalo con bilinear interpolation
        if x.size(2) != h_prev.size(2) or x.size(3) != h_prev.size(3):
            x = F.interpolate(x, size=(h_prev.size(2), h_prev.size(3)), mode='bilinear', align_corners=False)

        # Flusso zig-zag: sommare M_t con il livello superiore
        if m_upper is not None:
            m_prev = m_prev + torch.sigmoid(m_upper)  # Mantenere valori normalizzati

        # Convoluzioni per gate
        combined = self.conv_x(x) + self.conv_h(h_prev)
        i_c, i_m, f_c, f_m, g_c, g_m, o = torch.split(combined, self.hidden_dim, dim=1)

        # Memoria temporale C
        c_conv = self.conv_c(c_prev)
        f_c_c, i_c_c, o_c = torch.split(c_conv, self.hidden_dim, dim=1)
        delta_c = torch.sigmoid(i_c + i_c_c) * torch.tanh(g_c)
        c_new = torch.sigmoid(f_c + f_c_c) * c_prev + delta_c

        # Memoria spatiotemporale M
        m_conv = self.conv_m(m_prev)
        f_m_m, i_m_m, o_m = torch.split(m_conv, self.hidden_dim, dim=1)
        delta_m = torch.sigmoid(i_m + i_m_m) * torch.tanh(g_m)
        m_new = torch.sigmoid(f_m + f_m_m) * m_prev + delta_m

        # Fusione delle due memorie
        fused_states = self.conv_fusion(torch.cat([c_new, m_new], dim=1))
        h_new = torch.sigmoid(o) * torch.tanh(fused_states)

        # **Calcolo della decoupling loss con convoluzioni separate**
        delta_c_decoupled = self.conv_decouple_c(delta_c)  # W_decouple * (i_t ⊙ g_t)
        delta_m_decoupled = self.conv_decouple_m(delta_m)  # W_decouple * (i'_t ⊙ g'_t)

        # **Similarità coseno direttamente con F.cosine_similarity**
        cosine_similarity = F.cosine_similarity(delta_c_decoupled, delta_m_decoupled, dim=1)

        # **Perdita di decoupling (minimizzare la similarità)**
        decouple_loss = torch.mean(1 - cosine_similarity)  # Minimizza la similarità tra C e M

        return h_new, c_new, m_new, decouple_loss

class PredRNN_Block(nn.Module):
    def __init__(self, num_layers, num_hidden, filter_size):
        super().__init__()
        self.cells = nn.ModuleList()
        for _ in range(num_layers):
            self.cells.append(SpatiotemporalLSTMCell(
                input_dim=num_hidden,
                hidden_dim=num_hidden,
                filter_size=filter_size
            ))
        self.num_layers = num_layers

    def forward(self, input_sequence, h_t, c_t, m_t):
        seq_len = input_sequence.size(1)
        output_inner = []
        total_decouple_loss = 0.0

        for t in range(seq_len):
            ##########################
            # Fase 1: Propagazione bottom-up (da layer alto a basso)
            ##########################
            for l in reversed(range(self.num_layers)):
                # Input per il layer corrente
                if l == 0:
                    input_current = input_sequence[:, t]  # Input diretto per il primo layer
                else:
                    input_current = h_t[l-1]  # Output del layer precedente
                
                # Memoria del layer superiore (se esiste)
                m_upper = m_t[l+1] if l < self.num_layers - 1 else None
                
                # Aggiorna gli stati del layer
                h_new, c_new, m_new, cell_loss = self.cells[l](
                    input_current,
                    h_t[l],
                    c_t[l],
                    m_t[l],
                    m_upper
                )
                
                # Aggiorna gli stati persistenti
                h_t[l] = h_new
                c_t[l] = c_new
                m_t[l] = m_new
                total_decouple_loss += cell_loss  # Accumula la loss

            ##########################
            # Fase 2: Propagazione top-down (da layer basso ad alto)
            ##########################
            for l in range(1, self.num_layers):
                input_current = h_t[l-1]  # Output del layer precedente
                m_upper = m_t[l-1]  # Memoria del layer inferiore (aggiornata nella fase 1)
                
                # Aggiorna gli stati del layer
                h_new, c_new, m_new, cell_loss = self.cells[l](
                    input_current,
                    h_t[l],
                    c_t[l],
                    m_t[l],
                    m_upper
                )
                
                # Aggiorna gli stati persistenti
                h_t[l] = h_new
                c_t[l] = c_new
                m_t[l] = m_new
                total_decouple_loss += cell_loss  # Accumula la loss

            # Salva l'output dell'ultimo layer
            output_inner.append(h_t[-1])
        
        total_decouple_loss /= (seq_len * self.num_layers)

        return torch.stack(output_inner, dim=1), h_t, c_t, m_t, total_decouple_loss

class UNet_Encoder(nn.Module):
    def __init__(self, in_channels):
        super().__init__()
        self.enc1 = self.contract_block(in_channels, 64, 3, 1)
        self.enc2 = self.contract_block(64, 128, 3, 1)
        self.pool = nn.MaxPool2d(2)

    def contract_block(self, in_channels, out_channels, kernel_size, padding):
        return nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size, padding=padding),
            nn.BatchNorm2d(out_channels), 
            nn.ReLU(),
            nn.Conv2d(out_channels, out_channels, kernel_size, padding=padding),
            nn.BatchNorm2d(out_channels),  
            nn.ReLU()
        )

    def forward(self, x):
        x1 = self.enc1(x)
        x_pooled1 = self.pool(x1)
        x2 = self.enc2(x_pooled1)
        return x2, x1

class UNet_Decoder(nn.Module):
    def __init__(self, out_channels):
        super().__init__()
        self.upconv1 = nn.ConvTranspose2d(128, 64, kernel_size=2, stride=2)
        self.dec1 = self.expand_block(128, 64, 3, 1)
        self.upconv2 = nn.Conv2d(64, out_channels, kernel_size=1)

    def expand_block(self, in_channels, out_channels, kernel_size, padding):
        return nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size, padding=padding),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(),
            nn.Conv2d(out_channels, out_channels, kernel_size, padding=padding),
            nn.BatchNorm2d(out_channels),
            nn.ReLU()
        )

    def forward(self, x, skip):
        # Adattiamo la dimensione spaziale di x prima della concatenazione
        if x.size(2) != skip.size(2) or x.size(3) != skip.size(3):
            x = F.interpolate(x, size=(skip.size(2), skip.size(3)), mode='nearest')

        # Controlliamo che i canali di skip siano 64 (tagliamo se necessario)
        if skip.size(1) > 64:
            skip = skip[:, :64, :, :]

        # Controlliamo che i canali di x siano 64
        if x.size(1) > 64:
            x = x[:, :64, :, :]

        # Concatenazione corretta (ora ha esattamente 128 canali)
        x = torch.cat([x, skip], dim=1)
        
        x = self.dec1(x)            # Ora questa convoluzione riceve esattamente 128 canali
        x = self.upconv2(x)         # Ultima convoluzione per generare l'output
        return torch.sigmoid(x)

class RainPredRNN(nn.Module):
    def __init__(self, input_dim=1, num_hidden=64, num_layers=3, filter_size=3):
        super().__init__()
        self.encoder = UNet_Encoder(input_dim)
        self.decoder = UNet_Decoder(input_dim)
        self.rnn_block = PredRNN_Block(num_layers, num_hidden, filter_size)
        self.num_layers = num_layers
        self.num_hidden = num_hidden

    def forward(self, input_sequence, pred_length, teacher_forcing=False):
        batch_size, seq_len, _, h, w = input_sequence.size()
        device = input_sequence.device

        encoder_skips = []
        encoder_outputs = []
        for t in range(seq_len):
            enc_out, skip = self.encoder(input_sequence[:, t])
            encoder_outputs.append(enc_out)
            encoder_skips.append(skip)

        predictions = []
        total_decouple_loss = 0.0

        h_t = [torch.zeros(batch_size, self.num_hidden, h//4, w//4).to(device) 
            for _ in range(self.num_layers)]
        c_t = [torch.zeros(batch_size, self.num_hidden, h//4, w//4).to(device)
            for _ in range(self.num_layers)]
        m_t = [torch.zeros(batch_size, self.num_hidden, h//4, w//4).to(device)
            for _ in range(self.num_layers)]

        for t in range(seq_len + pred_length):
            if t < seq_len:
                x = encoder_outputs[t]
            else:
                if teacher_forcing and self.training:
                    x = encoder_outputs[t - seq_len]  # Usa il ground truth
                else:
                    prev_pred = predictions[-1] if predictions else input_sequence[:, -1]
                    x, skip = self.encoder(prev_pred)  # Usa la predizione precedente
                
                if self.training:
                    encoder_skips.append(skip)

                current_skip = skip

            rnn_out, h_t, c_t, m_t, decouple_loss = self.rnn_block(
                x.unsqueeze(1), h_t, c_t, m_t
            )
            total_decouple_loss += decouple_loss

            if t >= seq_len:
                pred = self.decoder(rnn_out.squeeze(1), current_skip)
                predictions.append(pred)

        return torch.stack(predictions, dim=1), total_decouple_loss

# === Inizializzazione pesi ===
def init_weights(m):
    if isinstance(m, (nn.Conv2d, nn.ConvTranspose2d)):
        nn.init.orthogonal_(m.weight)
        if m.bias is not None:
            nn.init.constant_(m.bias, 0)
    elif isinstance(m, nn.BatchNorm2d):
        nn.init.constant_(m.weight, 1)
        nn.init.constant_(m.bias, 0)

# === DataLoaders ===
def create_dataloaders(data_path, batch_size=4, num_workers=4):
    train_dataset = RadarDataset(os.path.join(data_path, 'train'), is_train=True)
    val_dataset = RadarDataset(os.path.join(data_path, 'val'), is_train=False)
    test_dataset = RadarDataset(os.path.join(data_path, 'test'), is_train=False)
    
    train_loader = DataLoader(
        train_dataset,
        batch_size=batch_size,
        shuffle=True,
        num_workers=num_workers,
        pin_memory=True,
        drop_last=True
    )
    
    val_loader = DataLoader(
        val_dataset,
        batch_size=batch_size,
        shuffle=False,
        num_workers=num_workers,
        pin_memory=True
    )
    
    test_loader = DataLoader(
        test_dataset,
        batch_size=1,
        shuffle=False,
        num_workers=num_workers
    )
    
    return train_loader, val_loader, test_loader

# === Metriche ===
def calculate_metrics(preds, targets, threshold_dbz=15):
    preds = preds.cpu().numpy().squeeze()  # Converti in NumPy
    targets = targets.cpu().numpy().squeeze()

    # Denormalizza i valori (da range [0, 1] a dBZ [0, 70])
    targets_dbz = np.clip(targets * 70.0, 0, 70)
    preds_dbz = np.clip(preds * 70.0, 0, 70)

    # Calcola MAE
    mae = np.mean(np.abs(preds_dbz - targets_dbz))

    # SSIM: Itera su batch e frames temporali
    ssim_values = []
    for b in range(preds_dbz.shape[0]):  # Batch loop
        for t in range(preds_dbz.shape[1]):  # Time step loop
            try:
                # Controllo NaN
                if np.isnan(preds_dbz[b, t]).any() or np.isnan(targets_dbz[b, t]).any():
                    print(f"Errore: NaN trovati a Batch {b}, Frame {t}")
                    ssim_values.append(0.0)
                    continue

                # Controllo immagini costanti
                if np.all(preds_dbz[b, t] == preds_dbz[b, t][0, 0]) or np.all(targets_dbz[b, t] == targets_dbz[b, t][0, 0]):
                    print(f"Errore: Immagine costante a Batch {b}, Frame {t}")
                    ssim_values.append(0.0)
                    continue

                # Controllo se l'immagine è completamente vuota
                if np.all(preds_dbz[b, t] == 0) or np.all(targets_dbz[b, t] == 0):
                    print(f"Errore: Immagine completamente vuota a Batch {b}, Frame {t}")
                    ssim_values.append(0.0)
                    continue

                # Controllo data_range
                data_range = targets_dbz[b, t].max() - targets_dbz[b, t].min()
                if data_range == 0:
                    print(f"Errore: data_range=0 a Batch {b}, Frame {t}. SSIM impostato a 0.")
                    ssim_values.append(0.0)
                    continue

                # Calcolo SSIM sicuro
                ssim_t = ssim(
                    preds_dbz[b, t], targets_dbz[b, t],  
                    data_range=data_range,  
                    win_size=5,  
                    multichannel=False
                )
                ssim_values.append(ssim_t)
            except Exception as e:
                print(f"Errore sconosciuto nel calcolo SSIM per Batch {b}, Frame {t}: {e}")
                ssim_values.append(0.0)  # Evita crash

    ssim_val = np.mean(ssim_values) if ssim_values else 0.0  # Media su tutti i frame

    # Binarizza con soglia di 15 dBZ
    preds_bin = (preds_dbz > threshold_dbz).astype(np.uint8)
    targets_bin = (targets_dbz > threshold_dbz).astype(np.uint8)

    # Calcola la matrice di confusione
    cm = confusion_matrix(targets_bin.flatten(), preds_bin.flatten(), labels=[0, 1])

    # Gestione robusta della matrice di confusione
    tn, fp, fn, tp = cm.ravel() if cm.shape == (2, 2) else (0, 0, 0, cm[0, 0] if cm.shape == (1, 1) else 0)

    # Calcola il CSI
    csi = tp / (tp + fp + fn + 1e-10)  # Evita divisione per zero

    return {
        'MAE': mae,
        'SSIM': ssim_val,
        'CSI': csi
    }

# === Training loop ===
def train_epoch(model, loader, optimizer, device):
    model.train()
    total_loss = 0.0

    for inputs, targets in loader:
        inputs, targets = inputs.to(device), targets.to(device)
        optimizer.zero_grad()

        with autocast():
            outputs, decouple_loss = model(inputs, PRED_LENGTH, teacher_forcing=True)

            ssim_loss = 0
            for t in range(outputs.shape[1]):  # Loop su ogni timestep
                # Assicurati che non ci siano NaN nei tensor
                if torch.isnan(outputs[:, t]).any() or torch.isnan(targets[:, t]).any():
                    print(f" Warning: NaN found in batch at timestep {t}")
                    continue  # Salta il frame con NaN

                # Calcolo SSIM per il frame corrente
                ssim_loss += 1 - criterion_ssim(outputs[:, t], targets[:, t])

            # Media su tutti i frame
            ssim_loss /= outputs.shape[1]

            # Loss totale
            loss = criterion_mae(outputs, targets) + 0.5 * ssim_loss + LAMBDA_DECOUPLE * (decouple_loss / (INPUT_LENGTH + PRED_LENGTH))

        scaler.scale(loss).backward()
        scaler.step(optimizer)
        scaler.update()
        total_loss += loss.item()

        torch.cuda.empty_cache()

    return total_loss / len(loader)

# === Valutazione ===
def evaluate(model, loader, device):
    model.eval()
    metrics = {'MAE': 0, 'SSIM': 0, 'CSI': 0}
    with torch.no_grad():
        for inputs, targets in loader:
            inputs, targets = inputs.to(device), targets.to(device)
            outputs, _ = model(inputs, PRED_LENGTH, teacher_forcing=True)
            
            # Calcola le metriche
            batch_metrics = calculate_metrics(outputs, targets)
            for k in metrics:
                metrics[k] += batch_metrics[k]
    
    # Media su tutto il dataset
    for k in metrics:
        metrics[k] /= len(loader)

    torch.cuda.empty_cache()
    return metrics

def load_images(image_paths):

    transform = transforms.Compose([
        transforms.Resize((256, 256)),  # Assicura che tutte le immagini abbiano la stessa dimensione
        transforms.ToTensor(),  # Converti in tensore
    ])

    images = []
    for path in image_paths:
        img = Image.open(path).convert("L")  # Converti in scala di grigi
        img = transform(img)  # Applica le trasformazioni
        img = img.unsqueeze(0)  # Aggiungi una dimensione per il canale (1, H, W)
        images.append(img)

    images = torch.stack(images, dim=0)  # Combina le immagini in un batch (6, 1, H, W)
    images = images.unsqueeze(0)  # Aggiungi dimensione batch: (1, 6, 1, H, W)

    return images.to(DEVICE)  # Sposta su GPU se disponibile

# === Salvataggio predizioni ===
def save_predictions(predictions, output_dir="outputs"):
    os.makedirs(output_dir, exist_ok=True)

    # Assicuriamoci che la forma sia corretta: (batch, timestep, H, W)
    preds = predictions.detach().cpu().numpy()  # Porta su CPU e converti in NumPy
    
    if preds.ndim == 5:  # Se ha forma (batch, timestep, 1, H, W), rimuoviamo il canale 1
        preds = preds.squeeze(2)  # Rimuove solo la dimensione del canale
        
    for batch_idx, seq in enumerate(preds):  # Per ogni sequenza nel batch
        for t in range(seq.shape[0]):  # Per ogni timestep della sequenza
            frame = (seq[t] * 70.0).clip(0, 255).astype(np.uint8)  # Converti in 8-bit
            img = Image.fromarray(frame)

            # Salviamo l'immagine con un nome chiaro
            filename = os.path.join(output_dir, f"pred_{batch_idx:04d}_t{t+1}.tiff")
            img.save(filename)

def save_predictions_single_test(predictions, output_dir="outputs/custom_test"):
    os.makedirs(output_dir, exist_ok=True)
    preds = predictions.detach().cpu().numpy()

    if preds.ndim == 5:  # Se ha forma (batch, timestep, 1, H, W), rimuoviamo il canale 1
        preds = preds.squeeze(2)

    for t in range(preds.shape[1]):  # Per ogni timestep della sequenza predetta
        frame = (preds[0, t] * 70.0).clip(0, 255).astype(np.uint8)  # Converti in 8-bit
        img = Image.fromarray(frame)
        filename = os.path.join(output_dir, f"pred_t{t+1}.tiff")
        img.save(filename)

# === Inizializzazione modello ===
torch.cuda.empty_cache()
model = RainPredRNN(input_dim=1, num_hidden=128, num_layers=3, filter_size=3)
model.apply(init_weights)
model = DataParallel(model).to(DEVICE)

# === Ottimizzatore e loss ===
optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)
scheduler = ReduceLROnPlateau(optimizer, mode='min', factor=0.5, patience=5)
criterion_mse = nn.MSELoss()
criterion_mae = nn.L1Loss()
criterion_ssim = SSIM(data_range=1.0, size_average=True, channel=1, win_size=5)

# === Supporto mixed-precision ===
scaler = GradScaler() # Per l'addestramento a precisione mista

# === Main ===
if __name__ == "__main__":
    # Configurazione percorsi
    DATA_PATH = "/home/f.demicco/RainPredRNN2/dataset"
    CHECKPOINT_DIR = "/home/f.demicco/RainPredRNN2/checkpoints"
    os.makedirs(CHECKPOINT_DIR, exist_ok=True)
    
    # Creazione dataloaders
    train_loader, val_loader, test_loader = create_dataloaders(DATA_PATH, BATCH_SIZE, NUM_WORKERS)
    
    # Training
    best_val_loss = float('inf')
    for epoch in range(NUM_EPOCHS):
        print(f"Epoch {epoch+1}/{NUM_EPOCHS}")

        train_loss = train_epoch(model, train_loader, optimizer, DEVICE)
        val_metrics = evaluate(model, val_loader, DEVICE)
        scheduler.step(val_metrics['MAE'])
            
        print(f"\tTrain Loss: {train_loss:.4f}")
        print(f"\tVal MAE: {val_metrics['MAE']:.4f}, SSIM: {val_metrics['SSIM']:.4f}, CSI: {val_metrics['CSI']:.4f}")
        
        if val_metrics['MAE'] < best_val_loss:
            best_val_loss = val_metrics['MAE']
            torch.save(model.state_dict(), os.path.join(CHECKPOINT_DIR, "best_model.pth"))
    
    # Test finale
    model.load_state_dict(torch.load(os.path.join(CHECKPOINT_DIR, "best_model.pth")))
    test_metrics = evaluate(model, test_loader, DEVICE)
    print("Test Results:")
    print(f"\tMAE: {test_metrics['MAE']:.4f}")
    print(f"\tSSIM: {test_metrics['SSIM']:.4f}")
    print(f"\tCSI: {test_metrics['CSI']:.4f}")

    # Salvataggio predizioni test
    os.makedirs("/home/f.demicco/RainPredRNN2/test_predictions", exist_ok=True)
    with torch.no_grad():
        for i, (inputs, targets) in enumerate(test_loader):
            inputs = inputs.to(DEVICE)
            outputs, _ = model(inputs, PRED_LENGTH)
            save_predictions(outputs, f"/home/f.demicco/RainPredRNN2/test_predictions/batch_{i:04d}")
        print("predizioni salvate correttamente")