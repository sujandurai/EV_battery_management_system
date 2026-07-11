import os
import torch
import torch.nn as nn

class Autoencoder(nn.Module):
    def __init__(self, input_dim=44, latent_dim=16):
        super(Autoencoder, self).__init__()
        self.encoder = nn.Sequential(
            nn.Linear(input_dim, 128),
            nn.ReLU(),
            nn.Linear(128, 64),
            nn.ReLU(),
            nn.Linear(64, 32),
            nn.ReLU(),
            nn.Linear(32, latent_dim),
            nn.ReLU()
        )
        self.decoder = nn.Sequential(
            nn.Linear(latent_dim, 32),
            nn.ReLU(),
            nn.Linear(32, 64),
            nn.ReLU(),
            nn.Linear(64, 128),
            nn.ReLU(),
            nn.Linear(128, input_dim)
        )
        
    def forward(self, x):
        latent = self.encoder(x)
        reconstructed = self.decoder(latent)
        return reconstructed, latent

def main():
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    
    pth_path = r"c:\ev vechile\sensor_trust_engine\sensor_autoencoder.pth"
    onnx_path = r"c:\ev vechile\sensor_trust_engine\sensor_autoencoder.onnx"
    
    print(f"Loading state dict from {pth_path}...")
    state_dict = torch.load(pth_path, map_location='cpu')
    
    # 44 is the input dimension for the 'reduced' feature set
    model = Autoencoder(input_dim=44, latent_dim=16)
    model.load_state_dict(state_dict)
    model.eval()
    print("Model loaded successfully!")
    
    # Re-export to ONNX with embedded parameters
    dummy_input = torch.randn(1, 44)
    print(f"Exporting model to ONNX format at {onnx_path}...")
    
    # Exclude external data by default (it's small so it will embed automatically)
    torch.onnx.export(
        model,
        dummy_input,
        onnx_path,
        export_params=True,
        opset_version=12,
        do_constant_folding=True,
        input_names=['input_features'],
        output_names=['reconstructed_features', 'latent_features'],
        dynamic_axes={
            'input_features': {0: 'batch_size'},
            'reconstructed_features': {0: 'batch_size'},
            'latent_features': {0: 'batch_size'}
        }
    )
    print("ONNX model successfully re-exported with embedded weights!")

if __name__ == "__main__":
    main()
