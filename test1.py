import os
steam_music_path = r"D:\SteamLibrary\steamapps\music"

def scan_music_files(directory):
    music_extensions = ['.m4a', '.mp3', '.wav', '.flac', '.aac', '.ogg']

    music_files = []

    for root, dirs, files in os.walk(directory):
        for file in files:
            if any(file.endswith(ext) for ext in music_extensions):
                music_files.append([0,os.path.join(root, file)])
    return music_files

def show_steam_music():
    """顯示steam上的音樂"""
    #輸出music路徑內的資料夾名稱
    i = os.listdir(steam_music_path)
    for j in i:
        print(j)
        z = os.path.join(steam_music_path,j)
        print("--------------------------------------------------------------")
        j = os.path.join(z,os.listdir(z)[0])
        if "WAV" in os.listdir(j):
            j = os.path.join(j,"WAV")
        #輸出music路徑內的檔案名稱
        for root, dirs, files in os.walk(os.path.join(steam_music_path,j)):
            for file in files:
                print(file)
        print("")


            






if __name__ == "__main__":
    show_steam_music()
