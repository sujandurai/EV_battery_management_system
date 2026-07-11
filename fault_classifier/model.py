import torch
import torch.nn as nn

class Attention(nn.Module):
    """
    Self-Attention layer (Bahdanau alignment) for sequence classification.
    Computes a weighted sum of hidden states over all sequence timesteps.
    ONNX and JIT trace compatible.
    """
    def __init__(self, hidden_dim):
        super().__init__()
        self.attn = nn.Linear(hidden_dim, hidden_dim)
        self.v = nn.Linear(hidden_dim, 1, bias=False)
        
    def forward(self, lstm_outputs):
        # lstm_outputs: (batch_size, seq_len, hidden_dim)
        u = torch.tanh(self.attn(lstm_outputs)) # (batch_size, seq_len, hidden_dim)
        scores = self.v(u).squeeze(-1) # (batch_size, seq_len)
        weights = torch.softmax(scores, dim=-1) # (batch_size, seq_len)
        
        # Batch matrix multiplication: (batch_size, 1, seq_len) x (batch_size, seq_len, hidden_dim)
        context = torch.bmm(weights.unsqueeze(1), lstm_outputs).squeeze(1) # (batch_size, hidden_dim)
        return context, weights

class LSTMClassifier(nn.Module):
    """
    Model A: Baseline Unidirectional LSTM.
    Outputs raw logits (compatible with CrossEntropyLoss/FocalLoss).
    """
    def __init__(self, input_dim, hidden_size, num_layers, dropout_rate, num_classes=12):
        super().__init__()
        self.num_layers = num_layers
        self.lstm = nn.LSTM(
            input_size=input_dim, 
            hidden_size=hidden_size, 
            num_layers=num_layers, 
            batch_first=True,
            dropout=dropout_rate if num_layers > 1 else 0.0
        )
        self.dropout1 = nn.Dropout(dropout_rate)
        self.fc1 = nn.Linear(hidden_size, 64)
        self.relu = nn.ReLU()
        self.dropout_fc = nn.Dropout(0.2)
        self.fc2 = nn.Linear(64, num_classes)
        
    def forward(self, x):
        out, _ = self.lstm(x)
        out = self.dropout1(out)
        final_timestep = out[:, -1, :] # Use final sequence step
        
        out = self.fc1(final_timestep)
        out = self.relu(out)
        out = self.dropout_fc(out)
        logits = self.fc2(out)
        return logits

class BiLSTMClassifier(nn.Module):
    """
    Model B: Bidirectional LSTM.
    Outputs raw logits.
    """
    def __init__(self, input_dim, hidden_size, num_layers, dropout_rate, num_classes=12):
        super().__init__()
        self.num_layers = num_layers
        self.lstm_hidden = max(16, hidden_size // 2)
        self.lstm = nn.LSTM(
            input_size=input_dim, 
            hidden_size=self.lstm_hidden, 
            num_layers=num_layers, 
            batch_first=True,
            bidirectional=True,
            dropout=dropout_rate if num_layers > 1 else 0.0
        )
        self.dropout1 = nn.Dropout(dropout_rate)
        self.fc1 = nn.Linear(self.lstm_hidden * 2, 64)
        self.relu = nn.ReLU()
        self.dropout_fc = nn.Dropout(0.2)
        self.fc2 = nn.Linear(64, num_classes)
        
    def forward(self, x):
        out, _ = self.lstm(x)
        out = self.dropout1(out)
        final_timestep = out[:, -1, :] # Contains concatenated forward/backward final states
        
        out = self.fc1(final_timestep)
        out = self.relu(out)
        out = self.dropout_fc(out)
        logits = self.fc2(out)
        return logits

class LSTMAttentionClassifier(nn.Module):
    """
    Model C: Unidirectional LSTM + Attention Layer.
    Outputs raw logits.
    """
    def __init__(self, input_dim, hidden_size, num_layers, dropout_rate, num_classes=12):
        super().__init__()
        self.num_layers = num_layers
        self.lstm = nn.LSTM(
            input_size=input_dim, 
            hidden_size=hidden_size, 
            num_layers=num_layers, 
            batch_first=True,
            dropout=dropout_rate if num_layers > 1 else 0.0
        )
        self.dropout1 = nn.Dropout(dropout_rate)
        self.attention = Attention(hidden_size)
        self.fc1 = nn.Linear(hidden_size, 64)
        self.relu = nn.ReLU()
        self.dropout_fc = nn.Dropout(0.2)
        self.fc2 = nn.Linear(64, num_classes)
        
    def forward(self, x):
        out, _ = self.lstm(x)
        out = self.dropout1(out)
        context, weights = self.attention(out) # Self-attention over all sequence steps
        
        out = self.fc1(context)
        out = self.relu(out)
        out = self.dropout_fc(out)
        logits = self.fc2(out)
        return logits

class BiLSTMAttentionClassifier(nn.Module):
    """
    Model D: Bidirectional LSTM + Attention Layer.
    Outputs raw logits.
    """
    def __init__(self, input_dim, hidden_size, num_layers, dropout_rate, num_classes=12):
        super().__init__()
        self.num_layers = num_layers
        self.lstm_hidden = max(16, hidden_size // 2)
        self.lstm = nn.LSTM(
            input_size=input_dim, 
            hidden_size=self.lstm_hidden, 
            num_layers=num_layers, 
            batch_first=True,
            bidirectional=True,
            dropout=dropout_rate if num_layers > 1 else 0.0
        )
        self.dropout1 = nn.Dropout(dropout_rate)
        self.attention = Attention(self.lstm_hidden * 2)
        self.fc1 = nn.Linear(self.lstm_hidden * 2, 64)
        self.relu = nn.ReLU()
        self.dropout_fc = nn.Dropout(0.2)
        self.fc2 = nn.Linear(64, num_classes)
        
    def forward(self, x):
        out, _ = self.lstm(x)
        out = self.dropout1(out)
        context, weights = self.attention(out) # Self-attention over concatenated bidirectional outputs
        
        out = self.fc1(context)
        out = self.relu(out)
        out = self.dropout_fc(out)
        logits = self.fc2(out)
        return logits
